# -*- coding: utf-8 -*-
"""几何核心：拼环、属性计算、包含关系、圆离散（弦高）、偏置（shapely）。

圆（CIRCLE）不走拼环，直接成环 —— 见《开源阅读清单》§2。
拼环用并查集：端点距离 < tol 归并为同一节点，度数=2 的链成环。
"""
from __future__ import annotations

import math
from collections import defaultdict

from shapely.geometry import Polygon
from shapely.ops import unary_union

from model import Contour, Entity


# ----------------------------------------------------------------------------
# 基础几何
# ----------------------------------------------------------------------------

def polygon_area(pts: list[tuple[float, float]]) -> float:
    """鞋带公式（带符号，CCW 为正）。"""
    if len(pts) < 3:
        return 0.0
    s = 0.0
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def polygon_perimeter(pts: list[tuple[float, float]], closed: bool = True) -> float:
    if len(pts) < 2:
        return 0.0
    seq = pts if (closed and pts[0] == pts[-1]) else (pts + [pts[0]] if closed else pts)
    return sum(math.dist(seq[i], seq[i + 1]) for i in range(len(seq) - 1))


def polygon_centroid(pts: list[tuple[float, float]]) -> tuple[float, float]:
    a = polygon_area(pts)
    if abs(a) < 1e-12:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (sum(xs) / len(xs), sum(ys) / len(ys))
    cx = cy = 0.0
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        cross = x1 * y2 - x2 * y1
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    return (cx / (6 * a), cy / (6 * a))


def bbox_of(pts: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def ensure_closed(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if pts and pts[0] != pts[-1]:
        return pts + [pts[0]]
    return pts


def point_in_ring(pt: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    """射线法判断点是否在闭合环内。ring 须 pts[0]==pts[-1]。"""
    x, y = pt
    inside = False
    n = len(ring) - 1
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xi:
                inside = not inside
    return inside


# ----------------------------------------------------------------------------
# 圆离散（弦高误差驱动，与黄金样例段数不咬死）
# ----------------------------------------------------------------------------

def discretize_circle(cx: float, cy: float, r: float,
                      chord_error: float = 0.05,
                      start_angle: float = math.pi,
                      ccw: bool = True) -> list[tuple[float, float]]:
    """整圆折线。起点角默认 π（圆心左侧），逆时针，闭合回起点。

    弦高 h = r(1-cos(θ/2)) → θ = 2·acos(1-h/r)，与最大步角 8° 取更密者。
    （chord_error=0.005 时 φ11→52 段、φ23→93 段，对齐黄金样例）
    """
    if r <= 0:
        return [(cx, cy)]
    h = min(chord_error, r * 0.5)
    theta = 2.0 * math.acos(1.0 - h / r)
    theta = min(theta, math.radians(8.0))
    n = max(8, math.ceil(2.0 * math.pi / theta))
    step = 2.0 * math.pi / n
    dirn = 1.0 if ccw else -1.0
    pts = []
    for i in range(n + 1):
        a = start_angle + dirn * step * i
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def discretize_arc(cx: float, cy: float, r: float,
                   a0: float, a1: float, chord_error: float = 0.05
                   ) -> list[tuple[float, float]]:
    """圆弧折线（含两端点，不重复）。"""
    if r <= 0:
        return []
    h = min(chord_error, r * 0.5)
    theta = 2.0 * math.acos(max(-1.0, 1.0 - h / r)) if h < r else math.pi
    theta = min(theta, math.radians(8.0))
    span = a1 - a0
    n = max(2, math.ceil(abs(span) / theta))
    return [(cx + r * math.cos(a0 + span * i / n),
             cy + r * math.sin(a0 + span * i / n)) for i in range(n + 1)]


# ----------------------------------------------------------------------------
# 并查集拼环
# ----------------------------------------------------------------------------

class _DSU:
    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, a: int) -> int:
        self.parent.setdefault(a, a)
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _snap_key(p: tuple[float, float], tol: float) -> tuple[int, int]:
    return (round(p[0] / tol), round(p[1] / tol))


def join_chains(entities: list[Entity], tol: float = 0.05) -> list[list[Entity]]:
    """把 LINE/ARC 折线按端点容差连成链。返回每条链的实体列表。

    实现：端点吸附到网格 → 并查集合并 → 同组实体按连接顺序串起。
    每个实体度数最多 2（链中前驱后继），成环条件首尾端点同节点。
    图层隔离：仅同图层实体才会相连成链/闭合（不同图层端点重合不拼）。
    """
    by_layer: dict[str, list[Entity]] = defaultdict(list)
    for e in entities:
        if not e.is_circle and len(e.points) >= 2:
            by_layer[e.layer].append(e)
    chains: list[list[Entity]] = []
    for ents in by_layer.values():
        chains.extend(_join_layer_chains(ents, tol))
    return chains


def _join_layer_chains(open_entities: list[Entity],
                       tol: float = 0.05) -> list[list[Entity]]:
    """同图层内：端点吸附 → 并查集 → 按连接顺序串链。"""
    chains: list[list[Entity]] = []

    # 节点表：吸附 key → 节点 id
    node_id: dict[tuple[int, int], int] = {}
    node_pts: list[tuple[float, float]] = []

    def get_node(p: tuple[float, float]) -> int:
        # 尝试邻近网格，避免边界抖动
        k = _snap_key(p, tol)
        for dk in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
            kk = (k[0] + dk[0], k[1] + dk[1])
            if kk in node_id:
                q = node_pts[node_id[kk]]
                if math.dist(p, q) <= tol:
                    return node_id[kk]
        node_id[k] = len(node_pts)
        node_pts.append(p)
        return node_id[k]

    ent_nodes: dict[int, tuple[int, int]] = {}  # idx → (起节点, 终节点)
    dsu = _DSU()
    for idx, e in enumerate(open_entities):
        n0 = get_node(e.points[0])
        n1 = get_node(e.points[-1])
        ent_nodes[idx] = (n0, n1)
        dsu.union(n0, n1)

    # 分组
    groups: dict[int, list[int]] = defaultdict(list)
    for idx in range(len(open_entities)):
        n0, _ = ent_nodes[idx]
        groups[dsu.find(n0)].append(idx)

    for _, idxs in groups.items():
        if not idxs:
            continue
        # 沿链走：建立节点 → 实体邻接
        node_ent: dict[int, list[int]] = defaultdict(list)
        for idx in idxs:
            n0, n1 = ent_nodes[idx]
            node_ent[n0].append(idx)
            node_ent[n1].append(idx)

        remaining = set(idxs)
        while remaining:
            # 任取一个端点实体开始
            start = next(iter(remaining))
            chain = [start]
            remaining.discard(start)
            # 向两端延伸
            for direction in (0, 1):  # 0=向起点方向, 1=向终点方向
                cur = start
                cur_end = ent_nodes[cur][direction]
                while True:
                    cands = [i for i in node_ent[cur_end] if i in remaining]
                    if not cands:
                        break
                    nxt = cands[0]
                    chain.append(nxt) if direction == 1 else chain.insert(0, nxt)
                    remaining.discard(nxt)
                    n0, n1 = ent_nodes[nxt]
                    # 穿过该实体到另一端
                    cur_end = n1 if cur_end == n0 else n0
            chains.append([open_entities[i] for i in chain])
    return chains


def chain_to_points(chain: list[Entity]) -> list[tuple[float, float]]:
    """把链中实体按连接顺序拼成点列（不强制闭合）。

    链首实体方向校正：若下一段接的是链首的起点侧，说明首段绘制方向与链方向相反，
    必须反转——否则点列会从链中部的分支点出发，
    先空走到首段自由端再折回（表现为刀路"前进又倒回"）。
    开放链起点归一：统一从最低（y 相同在最左）的端点起刀，与作图顺序无关；需要反向时用分组右键"矢量反向"。
    """
    if not chain:
        return []
    pts = list(chain[0].points)
    if len(chain) > 1:
        nxt = chain[1].points
        if min(math.dist(pts[0], nxt[0]), math.dist(pts[0], nxt[-1])) < \
           min(math.dist(pts[-1], nxt[0]), math.dist(pts[-1], nxt[-1])):
            pts.reverse()
    for e in chain[1:]:
        ep = e.points
        if math.dist(pts[-1], ep[0]) <= math.dist(pts[-1], ep[-1]):
            pts.extend(ep)
        else:
            pts.extend(reversed(ep))
    # 开放链起点归一：从最低（y 相同再最左）端点起刀
    if len(pts) >= 2 and pts[0] != pts[-1]:
        if (pts[-1][1], pts[-1][0]) < (pts[0][1], pts[0][0]):
            pts.reverse()
    return pts


# ----------------------------------------------------------------------------
# 轮廓构建与属性
# ----------------------------------------------------------------------------

def build_contours(entities: list[Entity], cfg=None) -> list[Contour]:
    """L1 实体 → L2 轮廓列表。圆独立成环；折线拼环。"""
    from model import JobConfig
    cfg = cfg or JobConfig()
    contours: list[Contour] = []
    n = 0

    circles = [e for e in entities if e.is_circle]
    others = [e for e in entities if not e.is_circle and len(e.points) >= 2]

    # 圆 → 环
    for e in circles:
        cx, cy, d = e.circle  # type: ignore[misc]
        pts = discretize_circle(cx, cy, d / 2.0, cfg.chord_error)
        c = Contour(
            id=f"C{n:03d}", entity_ids=[e.id], closed=True, pts=pts,
            area=math.pi * (d / 2) ** 2, perimeter=math.pi * d,
            centroid=(cx, cy), bbox=(cx - d / 2, cy - d / 2, cx + d / 2, cy + d / 2),
            is_circle=True, diameter=d, layer=e.layer,
        )
        contours.append(c)
        n += 1

    # 折线拼环
    for chain in join_chains(others, cfg.join_tol):
        pts = chain_to_points(chain)
        if len(pts) < 3:
            continue
        closed = math.dist(pts[0], pts[-1]) <= cfg.join_tol
        if closed:
            pts = ensure_closed(pts)
        c = Contour(
            id=f"C{n:03d}",
            entity_ids=[e.id for e in chain],
            closed=closed,
            pts=pts,
            area=abs(polygon_area(pts)) if closed else 0.0,
            perimeter=polygon_perimeter(pts, closed),
            centroid=polygon_centroid(pts) if closed else (
                sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)),
            bbox=bbox_of(pts),
            layer=chain[0].layer,
        )
        if closed:
            _classify_shape(c, cfg)
        contours.append(c)
        n += 1

    _build_containment(contours)
    return contours


def _classify_shape(c: Contour, cfg) -> None:
    """识别闭合环是否为矩形/圆角矩形/圆。"""
    # 圆度判定（离散折线环，周长含弦误差，阈值放宽）
    if c.area > 1e-9:
        roundness = 4 * math.pi * c.area / (c.perimeter ** 2)
        if roundness > cfg.roundness - 0.02 and not c.is_circle:
            c.is_circle = True
            c.diameter = (c.bbox[2] - c.bbox[0] + c.bbox[3] - c.bbox[1]) / 2

    # 矩形判定：obb 面积 ≈ 环面积，且边正交
    pts = c.pts[:-1] if c.pts and c.pts[0] == c.pts[-1] else c.pts
    if len(pts) < 4:
        return
    # 凸包 + 旋转卡壳简化：直接用 shapely minimum_rotated_rectangle
    try:
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        mrr = poly.minimum_rotated_rectangle
        mrr_area = mrr.area
        if mrr_area > 1e-9 and abs(poly.area - mrr_area) / mrr_area < 0.02:
            ext = list(mrr.exterior.coords)
            if len(ext) == 5:
                # 边与坐标轴夹角
                ang_ok = True
                for i in range(4):
                    x1, y1 = ext[i]
                    x2, y2 = ext[i + 1]
                    a = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 90.0
                    if min(a, 90 - a) > cfg.rect_angle_tol:
                        ang_ok = False
                        break
                if ang_ok:
                    c.is_rect = True
                    w = math.dist(ext[0], ext[1])
                    h = math.dist(ext[1], ext[2])
                    c.size = (min(w, h), max(w, h))
                    # 圆角：面积差 / 周长 → 估算角半径
                    dA = mrr_area - poly.area
                    if dA > 1e-6:
                        r_est = math.sqrt(max(dA, 0) / (4 - math.pi))
                        c.corner_r = round(r_est, 2)
    except Exception:
        pass


def _build_containment(contours: list[Contour]) -> None:
    """建立包含关系：形心在谁内部 → parent/child。"""
    for a in contours:
        if not a.closed:
            continue
        for b in contours:
            if a is b or not b.closed:
                continue
            # a 的形心在 b 内 且 a 面积更小 → a 是 b 的 child
            if a.area < b.area and point_in_ring(a.centroid, b.pts):
                # 取最小的这样的 b 作为直接父亲
                if a.parent_id is None:
                    a.parent_id = b.id
                    b.child_ids.append(a.id)
                else:
                    pass  # 已有父亲，b 是祖先


def mark_stock_outline(contours: list[Contour]) -> Contour | None:
    """最大面积闭合环标记 STOCK_OUTLINE。返回该轮廓或 None。"""
    closed = [c for c in contours if c.closed and c.area > 0]
    if not closed:
        return None
    stock = max(closed, key=lambda c: c.area)
    stock.is_stock_outline = True
    return stock


# ----------------------------------------------------------------------------
# 偏置（shapely）
# ----------------------------------------------------------------------------

def offset_closed_ring(pts: list[tuple[float, float]], delta: float) -> list[list[tuple[float, float]]]:
    """闭合环偏置 delta（负=向内）。返回偏置后的环列表（可能分裂/消失）。"""
    poly = Polygon(pts)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return []
    from shapely.geometry import JOIN_STYLE
    result = poly.buffer(delta, join_style=JOIN_STYLE.round,
                         quad_segs=8, mitre_limit=2.0)
    if result.is_empty:
        return []
    rings: list[list[tuple[float, float]]] = []
    geoms = [result] if result.geom_type == "Polygon" else list(result.geoms)
    for g in geoms:
        if g.geom_type != "Polygon" or g.area < 1e-9:
            continue
        ext = [(x, y) for x, y in g.exterior.coords]
        rings.append(ensure_closed(ext))
    return rings


def ring_children(pts: list[tuple[float, float]], contours: list[Contour],
                  tol: float = 0.05) -> list[Contour]:
    """返回形心落在环内的子轮廓（岛）。"""
    return [c for c in contours
            if c.closed and c.area > 0 and point_in_ring(c.centroid, pts)]
