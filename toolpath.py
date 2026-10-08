# -*- coding: utf-8 -*-
"""L4：刀路生成。Feature → 中性 Move[]，与控制器无关。

几何规格（由黄金 CNC 反推，见《项目需求总结更新》§7 与附录 A）：
- 螺旋孔：R_path = D/2 - Dtool/2 + hole_overcut(0.15)；起点圆心左侧，逆时针；
  每层先 G1 下刀再整圈走刀，层间不抬刀；结束 G0 抬刀。
- 口袋偏置：首圈偏置 = Dtool/2 - pocket_overcut(0.4)；后续步距 = Dtool×factor；
  圈序 outside_in（默认，外→内）/ inside_out（内→外）；每圈闭合后圈间就近衔接；
  每 Z 层重复同一 XY 路径。
- 直下刀（沉头刀已有孔）：定位 → G0 接近高度 → G1 到底 → 抬刀。
"""
from __future__ import annotations

import math

from model import (Contour, Feature, Tool, JobConfig, Move,
                   RAPID, FEED, PLUNGE, RETRACT,
                   SPIRAL, DIRECT_PLUNGE, OFFSET_POCKET, PARALLEL_POCKET,
                   CONTOUR, VECTOR_CUT)
import geom


# ----------------------------------------------------------------------------
# 螺旋孔（φ11 / φ23 / 钻孔）
# ----------------------------------------------------------------------------

def helical_hole(cx: float, cy: float, d_feat: float, tool: Tool,
                 depths: list[float], cfg: JobConfig,
                 overcut: float | None = None, ccw: bool = True,
                 allowance: float = 0.0) -> list[Move]:
    """螺旋下刀孔。R_path 对齐领跑：φ11+6mm 刀 → 2.6496。"""
    r_path = d_feat / 2.0 - tool.diameter / 2.0
    if overcut is None:
        overcut = cfg.hole_overcut
    r_path += overcut - allowance
    r_path = max(r_path, 0.05)

    pts = geom.discretize_circle(cx, cy, r_path, cfg.chord_error,
                                 start_angle=math.pi, ccw=ccw)
    moves: list[Move] = []
    moves.append(Move(RAPID, x=pts[0][0], y=pts[0][1], z=cfg.safe_z))
    for d in depths:
        moves.append(Move(PLUNGE, z=-d, f=tool.plunge_f))
        for (x, y) in pts[1:]:
            moves.append(Move(FEED, x=x, y=y, f=tool.feed_f))
    moves.append(Move(RETRACT, z=cfg.safe_z))
    return moves


# ----------------------------------------------------------------------------
# 直下刀（沉头孔：孔刀已开孔，沉头刀直接压下）
# ----------------------------------------------------------------------------

def direct_plunge(x: float, y: float, depth: float, tool: Tool,
                  cfg: JobConfig, approach_z: float = 1.0) -> list[Move]:
    """对齐排版沉头孔样例：G0 XY Z30 → G0 Z1 → G1 Z-depth → G0 Z30。"""
    return [
        Move(RAPID, x=x, y=y, z=cfg.safe_z),
        Move(RAPID, z=approach_z),
        Move(PLUNGE, z=-depth, f=tool.plunge_f),
        Move(RETRACT, z=cfg.safe_z),
    ]


# ----------------------------------------------------------------------------
# 清槽共用：可达区 / 边界绕行 / 安全连刀
# ----------------------------------------------------------------------------

def _pocket_poly(ring: list[tuple[float, float]],
                 islands: list[list[tuple[float, float]]]):
    """外轮廓 ∖ 避让岛（多岛 unary_union；自交岛 buffer(0) 修复；
    union 异常退化为逐岛 difference，禁止把岛静默丢成无岛）。"""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    outer = Polygon(ring)
    if not outer.is_valid:
        outer = outer.buffer(0)
    if islands:
        isles = []
        for i in islands:
            try:
                pg = Polygon(i)
                if not pg.is_valid:
                    pg = pg.buffer(0)
                if not pg.is_empty:
                    isles.append(pg)
            except Exception:
                continue
        try:
            outer = outer.difference(unary_union(isles))
        except Exception:
            for pg in isles:
                try:
                    outer = outer.difference(pg)
                except Exception:
                    pass
    return outer


def _boundary_linestrings(area) -> list:
    """可达区外环 + 岛洞环 → LineString 列表（连刀绕行路径候选）。"""
    from shapely.geometry import LineString
    out = []
    polys = [area] if area.geom_type == "Polygon" else [
        g for g in getattr(area, "geoms", []) if g.geom_type == "Polygon"]
    for g in polys:
        try:
            out.append(LineString(g.exterior.coords))
            for h in g.interiors:
                out.append(LineString(h.coords))
        except Exception:
            continue
    return out


def _link_route(p0, p1, tol_area, rings_ls):
    """切削深度连刀 p0→p1：直连 / 沿可达区边界（外环或岛洞环）绕行。

    安全判据 = 连线落在 tol_area（可达区 + 0.05 容差）内；岛禁区
    （岛.buffer(reach)）已从可达区挖除，故「在内」即同时保证不出袋、
    不穿任何岛。返回中间点列表（不含 p0/p1；[] = 可直连）；
    None = 无安全路径（须 RETRACT+RAPID，禁止斜穿岛）。
    """
    from shapely.geometry import LineString, Point
    from shapely.ops import substring

    if p0 == p1:
        return []
    ln = LineString([p0, p1])
    if ln.within(tol_area):
        return []
    best = None
    best_len = math.inf
    for ls in rings_ls:
        try:
            d0 = ls.project(Point(p0))
            d1 = ls.project(Point(p1))
            a0 = ls.interpolate(d0)
            a1 = ls.interpolate(d1)
            hop0 = LineString([p0, (a0.x, a0.y)])
            hop1 = LineString([(a1.x, a1.y), p1])
            if hop0.length > 1e-9 and not hop0.within(tol_area):
                continue
            if hop1.length > 1e-9 and not hop1.within(tol_area):
                continue
            fwd = substring(ls, d0, d1)
            bwd = substring(ls, d1, d0)
            if fwd.length <= bwd.length:
                walk = list(fwd.coords) if fwd.geom_type == "LineString" \
                    else [(a0.x, a0.y)]
            else:
                walk = list(reversed(list(bwd.coords))) \
                    if bwd.geom_type == "LineString" else [(a1.x, a1.y)]
            # 去除与 p0/p1 重复及相邻重合的微小段
            dedup = []
            for p in [p0] + walk + [p1]:
                if not dedup or math.dist(dedup[-1], p) > 1e-7:
                    dedup.append(p)
            total = hop0.length + min(fwd.length, bwd.length) + hop1.length
            if total < best_len:
                best_len = total
                best = dedup[1:-1]
        except Exception:
            continue
    return best


# ----------------------------------------------------------------------------
# 口袋偏置清除
# ----------------------------------------------------------------------------

def pocket_offset_paths(ring: list[tuple[float, float]],
                        islands: list[list[tuple[float, float]]],
                        first_offset: float, stepover: float,
                        max_rings: int = 50) -> list[list[tuple[float, float]]]:
    """外→内逐圈偏置。返回每圈的点环（未定向）。"""
    outer = _pocket_poly(ring, islands)
    if outer.is_empty:
        return []

    paths: list[list[tuple[float, float]]] = []
    cur = outer.buffer(-first_offset) if first_offset > 0 else outer
    n = 0
    while not cur.is_empty and n < max_rings:
        geoms = [cur] if cur.geom_type == "Polygon" else [
            g for g in cur.geoms if g.geom_type == "Polygon" and g.area > 1e-9]
        if not geoms:
            break
        for g in geoms:
            pts = [(x, y) for x, y in g.exterior.coords]
            paths.append(pts)
            # 内岛孔（环中有洞时，洞边也要走）
            for hole in g.interiors:
                hp = [(x, y) for x, y in hole.coords]
                if len(hp) >= 4:
                    paths.append(hp)
        n += 1
        cur = cur.buffer(-stepover)
    return paths


def pocket_moves(ring: list[tuple[float, float]],
                 islands: list[list[tuple[float, float]]],
                 tool: Tool, depths: list[float], cfg: JobConfig,
                 allowance_preset: float | None = None,
                 stepover: float | None = None,
                 direction: str = "outside_in",
                 entry_mode: str = "nearest") -> list[Move]:
    """口袋清除：每 Z 层重复同一组偏置环。stepover 显式指定时优先。

    direction：圈序——"outside_in"（默认，现网实机习惯：先大环后小环）/
    "inside_out"（先小环后大环）。
    entry_mode：圈间衔接——"nearest"（默认）每圈完整闭合后，下一圈起点旋到
    离本圈终点最近的顶点再整圈走刀；"min_xy" 恢复每圈最左下起点的旧行为
    （圈间易出现长斜拉，仅作兼容可选）。
    """
    overcut = cfg.pocket_overcut if allowance_preset is None else allowance_preset
    first_offset = tool.diameter / 2.0 - overcut
    first_offset = max(first_offset, 0.1)
    if stepover is None:
        stepover = tool.diameter * tool.stepover_factor
    rings = pocket_offset_paths(ring, islands, first_offset, stepover)
    if not rings:
        return []
    if direction == "inside_out":
        rings = list(reversed(rings))          # 内→外：小环在前

    # 统一逆时针（与领跑孔方向一致）
    oriented: list[list[tuple[float, float]]] = []
    for r in rings:
        pts = r[:-1] if r[0] == r[-1] else r
        if geom.polygon_area(pts + [pts[0]]) < 0:  # CW → 反转
            pts = list(reversed(pts))
        oriented.append(pts)

    # 安全连刀预计算：可达区（首圈外边界所围，岛禁区=岛外扩 reach 已挖除）
    outer = _pocket_poly(ring, islands)
    area = outer.buffer(-first_offset)
    if area.is_empty:
        tol_area = None
        rings_ls: list = []
    else:
        tol_area = area.buffer(0.05)
        rings_ls = _boundary_linestrings(area)

    # 圈间衔接：首圈锚定最左下（入刀点确定，与样例从左侧开始一致）；
    # 之后每圈起点 = 离上一圈终点（=其起点，已闭合）最近且能安全直连的
    # 顶点（连不上时退回最近顶点，由发射环节绕行/抬刀兜底）
    from shapely.geometry import LineString as _LS
    chained: list[list[tuple[float, float]]] = []
    cur: tuple[float, float] | None = None
    for i, pts in enumerate(oriented):
        if entry_mode == "min_xy" or i == 0:
            k = min(range(len(pts)), key=lambda j: (pts[j][0], pts[j][1]))
        else:
            order = sorted(range(len(pts)), key=lambda j: math.dist(cur, pts[j]))
            k = order[0]
            if tol_area is not None:
                # 就近顶点直连会穿岛/出袋时，换一个可安全直连的起点
                for j in order[:24]:
                    if _LS([cur, pts[j]]).within(tol_area):
                        k = j
                        break
        rp = ensure_pts_closed(pts[k:] + pts[:k])
        chained.append(rp)
        cur = rp[0]

    moves: list[Move] = []
    start = chained[0][0]
    moves.append(Move(RAPID, x=start[0], y=start[1], z=cfg.safe_z))
    cur = start
    for di, d in enumerate(depths):
        for ri, rp in enumerate(chained):
            if ri == 0 and di == 0:
                moves.append(Move(PLUNGE, z=-d, f=tool.plunge_f))
            else:
                # 环间连刀三级降级：安全直连 → 沿可达区边界绕行 → 抬刀快速定位。
                # 禁止未检查的斜切连刀（多避让岛袋曾由此穿岛过切）。
                route = _link_route(cur, rp[0], tol_area, rings_ls) \
                    if tol_area is not None else None
                if route is None:
                    moves.append(Move(RETRACT, z=cfg.safe_z))
                    moves.append(Move(RAPID, x=rp[0][0], y=rp[0][1]))
                    moves.append(Move(PLUNGE, z=-d, f=tool.plunge_f))
                else:
                    if ri == 0:
                        # 跨层衔接：先在原位加深再连刀（层间不抬刀语义）
                        moves.append(Move(PLUNGE, z=-d, f=tool.plunge_f))
                    for (x, y) in route:
                        moves.append(Move(FEED, x=x, y=y, f=tool.feed_f))
                    moves.append(Move(FEED, x=rp[0][0], y=rp[0][1],
                                       f=tool.feed_f))
            for (x, y) in rp[1:]:
                moves.append(Move(FEED, x=x, y=y, f=tool.feed_f))
            cur = rp[0]
    moves.append(Move(RETRACT, z=cfg.safe_z))
    return moves


# ----------------------------------------------------------------------------
# 平行清槽（刀轨平行 X/Y，zigzag 往复；行间衔接在区域内直连、否则抬刀）
# ----------------------------------------------------------------------------

def parallel_moves(ring: list[tuple[float, float]],
                   islands: list[list[tuple[float, float]]],
                   tool: Tool, depths: list[float], cfg: JobConfig,
                   axis: str = "X", stepover: float | None = None,
                   allowance_preset: float | None = None,
                   wall_trim: bool = True) -> list[Move]:
    """平行清槽：按行距逐行扫描区域，偶数行正向、奇数行反向（zigzag）。

    无避让岛：按行 zigzag（与历史行为完全一致）。
    有避让岛：改用全局「就近安全接龙」（方案 β，效果对齐 ArtCAM——先扫完
    一侧再换区，而不是每行左右跨岛）：每步在未走段两端点中选最近的、
    连线落在刀心可达区内的端点；全不可直连时取最近端点，发射环节沿区域
    边界绕行，绕不通才抬刀——任何情况下禁止 FEED 斜穿避让岛。
    wall_trim=True：每层扫描完后沿区域边界（含岛屿边）绕一圈精修内壁；
    精修环起点取离扫描终点最近的顶点（就近切入，不回区域左下角），
    多环时按就近顺序连接。
    """
    from shapely.geometry import LineString

    overcut = cfg.pocket_overcut if allowance_preset is None else allowance_preset
    reach = tool.diameter / 2.0 - overcut          # 刀心可达内缩距离
    if stepover is None:
        stepover = tool.diameter * tool.stepover_factor
    if stepover <= 0:
        return []

    outer = _pocket_poly(ring, islands)
    area = outer.buffer(-reach) if reach > 0 else outer
    if area.is_empty:
        return []

    minx, miny, maxx, maxy = area.bounds
    horiz = (axis == "X")          # True: 走刀沿 X，行沿 Y 分布
    lo, hi = (miny, maxy) if horiz else (minx, maxx)
    lo, hi = lo + 0.01, hi - 0.01  # 微内缩：避免扫描线贴边界被 shapely 判空
    span_line = (maxx - minx) if horiz else (maxy - miny)
    if span_line <= 0:
        return []

    # 行坐标：从 lo 起每 stepover 一行，最后不足一行距时贴 hi 收尾
    lines: list[float] = []
    c = lo
    while c < hi - 1e-9:
        lines.append(c)
        c += stepover
    if not lines or lines[-1] < hi - 1e-9:
        lines.append(hi)

    # 每行的切削段（与区域求交；岛屿会把行打断成多段）
    seg_rows: list[list[list[tuple[float, float]]]] = []
    for v in lines:
        if horiz:
            ln = LineString([(minx - 1.0, v), (maxx + 1.0, v)])
        else:
            ln = LineString([(v, miny - 1.0), (v, maxy + 1.0)])
        inter = area.intersection(ln)
        gs: list = []
        if not inter.is_empty:
            if inter.geom_type == "LineString":
                gs = [inter]
            elif hasattr(inter, "geoms"):      # MultiLineString / GeometryCollection
                gs = list(inter.geoms)
        parts = [list(g.coords) for g in gs
                 if g.geom_type == "LineString" and g.length > 1e-9]
        # 统一段方向（shapely 返回方向不定）：horiz 按 x 升序、vert 按 y 升序
        if horiz:
            parts = [s if s[0][0] <= s[-1][0] else list(reversed(s))
                     for s in parts]
        else:
            parts = [s if s[0][1] <= s[-1][1] else list(reversed(s))
                     for s in parts]
        if parts:
            parts.sort(key=lambda s: (s[0][0], s[0][1]))
            seg_rows.append(parts)
    if not seg_rows:
        return []

    # zigzag 基准序（无避让岛时沿用，保证与历史行为一致）：
    # 奇数行整行倒序且段内坐标反转
    # 全部扫描段展平（行序·行内位置序；方向已按走刀轴归一升序）
    all_segs: list[list[tuple[float, float]]] = [
        s for parts in seg_rows for s in parts]

    def _zigzag_seq() -> list[tuple[int, bool]]:
        """无岛固定序：(段id, 是否反向)，与历史 zigzag 输出一致。"""
        seq: list[tuple[int, bool]] = []
        base = 0
        for i, parts in enumerate(seg_rows):
            m = len(parts)
            if i % 2 == 0:
                seq.extend((base + j, False) for j in range(m))
            else:
                seq.extend((base + j, True) for j in range(m - 1, -1, -1))
            base += m
        return seq

    def _plan_layer(entry):
        """层内走刀序列 (段id, 是否反向)。

        无岛 → 固定 zigzag 序。有岛 → 全局就近安全接龙：每步在未走段
        两端点中选「连线落在可达区内」的最近端点（先一侧扫完再换区，
        避免每行跨岛）；全不可直连时取最近端点，发射环节将尝试沿区域
        边界绕行、绕不通则抬刀。entry=None（首层）固定从首行首段正向起。
        """
        if not islands:
            return _zigzag_seq()
        todo = list(range(len(all_segs)))
        out: list[tuple[int, bool]] = []
        cur = entry
        if cur is None:
            out.append((0, False))
            todo.remove(0)
            cur = all_segs[0][-1]
        while todo:
            cands: list[tuple[float, int, bool]] = []
            for si in todo:
                s = all_segs[si]
                cands.append((math.dist(cur, s[0]), si, False))
                cands.append((math.dist(cur, s[-1]), si, True))
            cands.sort(key=lambda t: t[0])
            pick = None
            for dist, si, rev in cands:
                pt = all_segs[si][-1] if rev else all_segs[si][0]
                if dist <= 1e-9 or LineString([cur, pt]).within(tol_area):
                    pick = (si, rev)
                    break
            if pick is None:
                # 全部端点不可直连（换区）：就近抬刀/绕行的落点
                pick = (cands[0][1], cands[0][2])
            out.append(pick)
            todo.remove(pick[0])
            s = all_segs[pick[0]]
            cur = s[0] if pick[1] else s[-1]
        return out

    # 内壁精修环：每层扫描完后绕可达区域边界一圈（含岛屿边），清内壁。
    # 开环存储、起点不在此固定——走刀时按"离当前刀位最近顶点"旋转（见下方循环）
    trim_rings: list[list[tuple[float, float]]] = []
    if wall_trim:
        polys = [area] if area.geom_type == "Polygon" else [
            g for g in getattr(area, "geoms", [])
            if g.geom_type == "Polygon" and g.area > 1e-9]
        for g in polys:
            g = g.simplify(0.01) if hasattr(g, "simplify") else g
            rings_xy = [g.exterior.coords] + [h.coords for h in g.interiors]
            for coords in rings_xy:
                pts = [(x, y) for x, y in coords]
                if pts and pts[0] == pts[-1]:
                    pts = pts[:-1]
                if len(pts) < 3:
                    continue
                if geom.polygon_area(pts + [pts[0]]) < 0:   # CW → 统一逆时针
                    pts = list(reversed(pts))
                trim_rings.append(pts)

    tol_area = area.buffer(0.05)   # 连接线落点容差（不得放宽，避免穿岛）
    rings_ls = _boundary_linestrings(area)
    moves: list[Move] = []
    cur_xy: tuple[float, float] | None = None
    prev_layer = -1
    first = True

    def emit(seg: list[tuple[float, float]], d: float, di: int):
        """输出一段走刀：直连 / 沿可达区边界绕行（切削深度衔接）；
        不可安全连刀才抬刀快速定位。跨层保持「先连刀到位、再下刀」语义。"""
        nonlocal cur_xy, first, prev_layer
        start, end = seg[0], seg[-1]
        if first:
            moves.append(Move(RAPID, x=start[0], y=start[1], z=cfg.safe_z))
            moves.append(Move(PLUNGE, z=-d, f=tool.plunge_f))
            first = False
        elif cur_xy != start:
            cross = (prev_layer != di)               # 跨层（z 变化）
            route = _link_route(cur_xy, start, tol_area, rings_ls)
            if route is None:
                moves.append(Move(RETRACT, z=cfg.safe_z))
                moves.append(Move(RAPID, x=start[0], y=start[1]))
                moves.append(Move(PLUNGE, z=-d, f=tool.plunge_f))
            else:
                for (x, y) in route:
                    moves.append(Move(FEED, x=x, y=y, f=tool.feed_f))
                moves.append(Move(FEED, x=start[0], y=start[1], f=tool.feed_f))
                if cross:
                    moves.append(Move(PLUNGE, z=-d, f=tool.plunge_f))
        elif prev_layer != di:
            moves.append(Move(PLUNGE, z=-d, f=tool.plunge_f))  # 同点跨层补下刀
        prev_layer = di
        for pt in seg[1:]:
            moves.append(Move(FEED, x=pt[0], y=pt[1], f=tool.feed_f))
        cur_xy = end

    for di, d in enumerate(depths):
        # 1) 该层全部扫描段：无岛固定 zigzag；有岛就近安全接龙
        #    （层间以上一层终点为入口就近重排）
        entry = None if first else cur_xy
        for si, rev in _plan_layer(entry):
            seg = list(reversed(all_segs[si])) if rev else all_segs[si]
            emit(seg, d, di)
        # 2) 内壁精修环：多环就近排序，起点旋到离当前刀位最近顶点，整圈闭合
        if wall_trim and trim_rings:
            pending = list(trim_rings)
            while pending:
                bi = min(range(len(pending)), key=lambda i: min(
                    math.dist(cur_xy, p) for p in pending[i]))
                pts = pending.pop(bi)
                k = min(range(len(pts)), key=lambda j: math.dist(cur_xy, pts[j]))
                emit(ensure_pts_closed(pts[k:] + pts[:k]), d, di)
    if not first:
        moves.append(Move(RETRACT, z=cfg.safe_z))
    return moves


def vector_cut_moves(pts: list[tuple[float, float]], tool: Tool,
                     depths: list[float], cfg: JobConfig,
                     side: str = "center") -> list[Move]:
    """沿矢量加工（开放链）：刀心在矢量线左侧 / 右侧 / 居中。

    左右 = 开放路径法向偏置 D/2（沿矢量行进方向），居中 = 刀心压线。
    每层沿偏置线正向走刀，层间抬刀回起点重新下刀。
    """
    path = list(pts)
    if path and path[0] == path[-1]:
        path = path[:-1]
    if len(path) < 2:
        return []
    if side in ("left", "right"):
        from shapely.geometry import LineString
        delta = tool.diameter / 2.0 if side == "left" else -tool.diameter / 2.0
        ls = LineString(path).offset_curve(delta)
        path = [(x, y) for x, y in ls.coords]
        if len(path) < 2:
            return []
    moves: list[Move] = [Move(RAPID, x=path[0][0], y=path[0][1], z=cfg.safe_z)]
    for i, d in enumerate(depths):
        moves.append(Move(PLUNGE, z=-d, f=tool.plunge_f))
        for (x, y) in path[1:]:
            moves.append(Move(FEED, x=x, y=y, f=tool.feed_f))
        moves.append(Move(RETRACT, z=cfg.safe_z))
        if i < len(depths) - 1:
            moves.append(Move(RAPID, x=path[0][0], y=path[0][1]))
    return moves


def ensure_pts_closed(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    return pts + [pts[0]] if pts[0] != pts[-1] else pts


# ----------------------------------------------------------------------------
# 轮廓（内/外）
# ----------------------------------------------------------------------------

def contour_moves(ring: list[tuple[float, float]], tool: Tool,
                  depths: list[float], cfg: JobConfig,
                  side: str = "inside", allowance: float = 0.0) -> list[Move]:
    """轮廓加工。side=inside 刀心向内偏 D/2；outside 向外。"""
    off = tool.diameter / 2.0 - allowance
    delta = -off if side == "inside" else off
    rings = geom.offset_closed_ring(ring, delta)
    if not rings:
        return []
    pts0 = rings[0]
    moves = [Move(RAPID, x=pts0[0][0], y=pts0[0][1], z=cfg.safe_z)]
    for d in depths:
        moves.append(Move(PLUNGE, z=-d, f=tool.plunge_f))
        for (x, y) in pts0[1:]:
            moves.append(Move(FEED, x=x, y=y, f=tool.feed_f))
    moves.append(Move(RETRACT, z=cfg.safe_z))
    return moves


# ----------------------------------------------------------------------------
# 特征 → Move[] 总调度
# ----------------------------------------------------------------------------

def _collect_islands(contour: Contour, all_contours: list[Contour],
                     cfg: JobConfig) -> list[list[tuple[float, float]]]:
    """收集口袋避让岛：几何包含识别的 child 环 ∪ 「避让岛」图层内含环。

    图层岛不依赖 child 识别（外框常抢先认领父级），按形心落在口袋内或
    整体被口袋包含（容差 join_tol）判定；袋外/无效环跳过。
    两种来源按形心去重后合并。
    """
    from shapely.geometry import Polygon, Point

    by_id = {c.id: c for c in all_contours}
    islands: list[list[tuple[float, float]]] = []
    taken: list[tuple[float, float]] = []          # 已收集岛形心（去重）
    # 1) 几何包含识别的子环（原有 child 岛逻辑）
    for cid in contour.child_ids:
        ch = by_id.get(cid)
        if ch is not None and ch.closed and ch.area > 0 and not ch.is_stock_outline:
            islands.append(ch.pts)
            taken.append(ch.centroid)
    if not contour.closed:
        return islands
    # 2) 「避让岛」图层环：位于口袋内部即避让（可多个）
    try:
        poly = Polygon(contour.pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        tol_poly = poly.buffer(cfg.join_tol)
    except Exception:
        return islands
    for c in all_contours:
        if not c.is_island or not c.closed or c.area <= 0 or c.id == contour.id:
            continue
        if any(math.dist(c.centroid, g) <= cfg.join_tol for g in taken):
            continue                               # 与已收集岛几何重复
        try:
            inside = tol_poly.contains(Point(c.centroid)) or \
                Polygon(c.pts).within(tol_poly)
        except Exception:
            inside = False                         # 无效环跳过
        if inside:
            islands.append(c.pts)
            taken.append(c.centroid)
    return islands


def gen_feature_moves(feature: Feature, contour: Contour,
                      all_contours: list[Contour],
                      tool: Tool, cfg: JobConfig) -> list[Move]:
    """单个特征生成刀路。不含换刀；tool 由调用方按 params.tool_id 取。"""
    p = feature.params
    depths = p.pass_depths()
    if not depths:
        return []

    if p.strategy == SPIRAL and contour.is_circle and contour.diameter:
        overcut = cfg.hole_overcut - p.allowance
        return helical_hole(contour.centroid[0], contour.centroid[1],
                            contour.diameter, tool, depths, cfg,
                            overcut=overcut, ccw=p.climb)

    if p.strategy == DIRECT_PLUNGE:
        ap = p.approach_z if p.approach_z is not None else 1.0
        return direct_plunge(contour.centroid[0], contour.centroid[1],
                             p.depth, tool, cfg, approach_z=ap)

    if p.strategy in (OFFSET_POCKET, PARALLEL_POCKET):
        # 岛：child 识别环 ∪ 「避让岛」图层内含环（去重合并）
        islands = _collect_islands(contour, all_contours, cfg)
        sv = p.stepover_mm if p.stepover_mm else tool.diameter * p.stepover_factor
        if p.strategy == PARALLEL_POCKET:
            return parallel_moves(contour.pts, islands, tool, depths, cfg,
                                  axis=p.parallel_axis, stepover=sv,
                                  allowance_preset=cfg.pocket_overcut - p.allowance)
        return pocket_moves(contour.pts, islands, tool, depths, cfg,
                            allowance_preset=cfg.pocket_overcut - p.allowance,
                            stepover=sv, direction=p.pocket_direction)

    if p.strategy == CONTOUR and contour.closed:
        side = "outside" if contour.is_stock_outline else "inside"
        return contour_moves(contour.pts, tool, depths, cfg,
                             side=side, allowance=p.allowance)

    if p.strategy == VECTOR_CUT:
        return vector_cut_moves(contour.pts, tool, depths, cfg,
                                 side=p.vector_side)

    # on_vector / 开放矢量 / 其它：沿中心线
    #   轮廓 → 沿矢量方向；螺旋下刀 → 逆矢量方向（两遍双向走刀可消除让刀痕）
    moves: list[Move] = []
    pts = list(contour.pts)
    if p.strategy == SPIRAL and pts and pts[0] != pts[-1]:
        pts = list(reversed(pts))          # 开放矢量 + 螺旋下刀：逆矢量行进
    moves.append(Move(RAPID, x=pts[0][0], y=pts[0][1], z=cfg.safe_z))
    for d in depths:
        moves.append(Move(PLUNGE, z=-d, f=tool.plunge_f))
        for (x, y) in pts[1:]:
            moves.append(Move(FEED, x=x, y=y, f=tool.feed_f))
        if len(depths) > 1 and d != depths[-1]:
            moves.append(Move(RAPID, z=cfg.safe_z))
            moves.append(Move(RAPID, x=pts[0][0], y=pts[0][1]))
    moves.append(Move(RETRACT, z=cfg.safe_z))
    return moves


# ----------------------------------------------------------------------------
# 覆盖区预览：刀心轨迹 × 刀径 → 平面覆盖区域（纯预览用，不影响刀路生成）
# ----------------------------------------------------------------------------

def covered_area(moves, tool_diameter: float):
    """FEED 段沿刀径缓冲的覆盖区并集（画布预览用，判断清料干净/漏切死角）。

    只统计 kind==FEED 且 x、y 均不为 None 的段（RAPID/RETRACT 抬刀快移、
    PLUNGE 纯下刀不产生 XY 覆盖）；prev 追踪所有带 x/y 段的当前位置，
    使首个 FEED 段（起点来自下刀 RAPID）也能生成胶囊。
    每段 LineString([prev, cur]).buffer(D/2)，收集后一次性 unary_union
    （禁止逐段 union，避免 O(n²)）。
    无有效段或结果为空返回 None；否则返回 Polygon / MultiPolygon。
    """
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    caps = []
    prev = None
    for m in moves:
        if m.x is None or m.y is None:
            continue                      # 仅 z 的段（PLUNGE/RETRACT/层间抬刀）不动 prev
        if m.kind != FEED:
            prev = (m.x, m.y)             # 快移/下刀只更新当前位置，不产生覆盖
            continue
        cur = (m.x, m.y)
        if prev is not None:
            try:
                caps.append(LineString([prev, cur]).buffer(tool_diameter / 2))
            except Exception:  # noqa: BLE001 —— 单段缓冲失败跳过，不影响整体
                pass
        prev = cur
    if not caps:
        return None
    try:
        area = unary_union(caps)
    except Exception:  # noqa: BLE001
        return None
    return None if area.is_empty else area
