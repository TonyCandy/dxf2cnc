# -*- coding: utf-8 -*-
"""L1：DXF 读取。编码 GBK 优先回退、单位、实体离散、图层角色分类。

支持的加工实体：CIRCLE / LINE / ARC / LWPOLYLINE / POLYLINE。
HATCH/DIMENSION/TEXT/MTEXT/INSERT/SPLINE 保留在 ignore 实体里（画布可显示，不加工）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import ezdxf

from model import Entity


# ----------------------------------------------------------------------------
# 图层角色
# ----------------------------------------------------------------------------

ROLE_DRILL = "drill"            # 钻孔层（φ11 / PH / 孔 / 纯数字直径）
ROLE_TOOL = "tool"              # 刀号层（T01/T02…）
ROLE_SECTION = "section"        # 剖面线钻孔层
ROLE_IGNORE = "ignore"          # 尺寸线/中心线/虚线/细实线/文字
ROLE_GEOM = "geom"              # 兜底加工几何（粗实线层等）
ROLE_ZERO = "zero"              # 图层 0（外框 + 可能的内槽）
ROLE_ISLAND = "island"          # 避让岛层（口袋清料时保留的中间岛）


@dataclass
class DxfData:
    path: str = ""
    entities: list[Entity] = field(default_factory=list)     # 参与加工/拼环的实体
    ignored: list[Entity] = field(default_factory=list)      # 参考层实体（画布用）
    layer_roles: dict[str, str] = field(default_factory=dict)
    unit_is_inch: bool = False
    warnings: list[str] = field(default_factory=list)
    all_layers: list[str] = field(default_factory=list)


def _classify_layer(name: str, ignore_names: list[str]) -> str:
    n = name.strip()
    low = n.lower()
    # 显式忽略层
    for ig in ignore_names:
        if n == ig or n.startswith(ig):
            return ROLE_IGNORE
    if any(k in low for k in ("dim", "center", "text", "hatch", "虚线", "细实线", "标注")):
        return ROLE_IGNORE
    if n == "0":
        return ROLE_ZERO
    # 避让岛层：口袋清料时保留的中间岛（不单独生成刀路）
    if "避让岛" in n:
        return ROLE_ISLAND
    # 剖面线层
    if "剖面" in n:
        return ROLE_SECTION
    # 钻孔层：φ/𝛟/Φ/ｆ/⌀ / PH / 孔 / 纯数字
    if any(ch in n for ch in ("φ", "𝛟", "Φ", "ｆ", "⌀")) or "ph" in low or "孔" in n:
        return ROLE_DRILL
    stripped = n.replace("φ", "").replace("𝛟", "").replace("Φ", "").replace("ｆ", "").replace("⌀", "").strip()
    if stripped.replace(".", "").replace(",", "").isdigit():
        return ROLE_DRILL
    # 刀号层：T01 / T02 / T1 / T6 …
    if len(low) >= 2 and low[0] == "t" and low[1:].isdigit():
        return ROLE_TOOL
    # 兜底几何（粗实线层等）
    return ROLE_GEOM


def layer_diameter_hint(layer: str) -> float | None:
    """从图层名提取直径提示：φ11 / 𝛟11 / Φ11 / ｆ11 / ⌀11 → 11.0。纯数字图层名不再解析。"""
    n = layer.strip()
    for ch in ("φ", "𝛟", "Φ", "ｆ", "⌀"):
        if ch in n:
            tail = n.split(ch)[-1].strip()
            try:
                return float(tail)
            except ValueError:
                pass
    return None


def layer_tool_hint(layer: str) -> int | None:
    """图层名刀号提示：'T01' → 1。"""
    low = layer.strip().lower()
    if len(low) >= 2 and low[0] == "t" and low[1:].isdigit():
        return int(low[1:])
    return None


# ----------------------------------------------------------------------------
# 读取主入口
# ----------------------------------------------------------------------------

PROCESSABLE = {"CIRCLE", "LINE", "ARC", "LWPOLYLINE", "POLYLINE"}
IGNORED_TYPES = {"HATCH", "DIMENSION", "TEXT", "MTEXT", "INSERT", "SPLINE",
                 "POINT", "ELLIPSE", "LEADER", "SOLID"}


def read_dxf(path: str | Path, cfg=None) -> DxfData:
    """读 DXF → DxfData。编码：先默认探测，失败回退 GBK。"""
    from model import JobConfig
    cfg = cfg or JobConfig()
    p = Path(path)
    doc = None
    err: Exception | None = None
    for enc in (None, "gbk", "utf-8"):
        try:
            if enc is None:
                doc = ezdxf.readfile(str(p))
            else:
                doc = ezdxf.readfile(str(p), encoding=enc)
            break
        except Exception as e:  # noqa: BLE001
            err = e
            doc = None
    if doc is None:
        raise ValueError(f"无法读取 DXF {p}: {err}")

    data = DxfData(path=str(p))

    # 单位检查
    insunits = doc.header.get("$INSUNITS", 4)
    if insunits == 1:  # inch
        data.unit_is_inch = True
        data.warnings.append("DXF 单位为英寸，已按 mm 处理，请核对图纸")
    extmin = doc.header.get("$EXTMIN", (0, 0, 0))
    extmax = doc.header.get("$EXTMAX", (0, 0, 0))

    msp = doc.modelspace()
    scale = 25.4 if data.unit_is_inch else 1.0

    idx = 0
    for e in msp:
        etype = e.dxftype()
        layer = e.dxf.layer
        if layer not in data.layer_roles:
            data.layer_roles[layer] = _classify_layer(layer, cfg.ignore_layers)
            data.all_layers.append(layer)
        role = data.layer_roles[layer]

        ent = _entity_from(e, idx, scale)
        if ent is None:
            continue
        if etype in IGNORED_TYPES or role == ROLE_IGNORE:
            data.ignored.append(ent)
        elif etype in PROCESSABLE:
            data.entities.append(ent)
        else:
            data.ignored.append(ent)
        idx += 1

    if not data.entities:
        data.warnings.append("未发现可加工实体（LINE/ARC/CIRCLE/POLYLINE）")
    return data


def _entity_from(e, idx: int, scale: float = 1.0) -> Entity | None:
    etype = e.dxftype()
    handle = str(e.dxf.handle)
    layer = e.dxf.layer

    def P(x, y):
        return (x * scale, y * scale)

    if etype == "CIRCLE":
        c = e.dxf.center
        d = float(e.dxf.radius) * 2 * scale
        return Entity(id=f"E{idx:04d}", etype="CIRCLE", layer=layer,
                      points=[(c[0] * scale, c[1] * scale)],
                      circle=(c[0] * scale, c[1] * scale, d),
                      source_handle=handle)

    if etype == "LINE":
        s, t = e.dxf.start, e.dxf.end
        return Entity(id=f"E{idx:04d}", etype="LINE", layer=layer,
                      points=[P(s[0], s[1]), P(t[0], t[1])],
                      source_handle=handle)

    if etype == "ARC":
        c = e.dxf.center
        r = float(e.dxf.radius) * scale
        a0 = math.radians(float(e.dxf.start_angle))
        a1 = math.radians(float(e.dxf.end_angle))
        if a1 <= a0:
            a1 += 2 * math.pi
        # 引用 geom 里的离散，避免循环依赖，这里就地实现
        from geom import discretize_arc
        pts = discretize_arc(c[0] * scale, c[1] * scale, r, a0, a1)
        return Entity(id=f"E{idx:04d}", etype="ARC", layer=layer,
                      points=pts, source_handle=handle)

    if etype in ("LWPOLYLINE", "POLYLINE"):
        pts: list[tuple[float, float]] = []
        if etype == "LWPOLYLINE":
            raw = list(e.get_points("xyb"))  # x, y, bulge
            closed_flag = bool(e.closed)
            for i, (x, y, b) in enumerate(raw):
                pts.append(P(x, y))
                if abs(b) > 1e-9 and i < len(raw) - 1 or (abs(b) > 1e-9 and closed_flag and i == len(raw) - 1):
                    x2, y2, _ = raw[(i + 1) % len(raw)]
                    pts.extend(_bulge_arc(P(x, y), P(x2, y2), b, scale))
        else:
            closed_flag = bool(e.is_closed)
            for v in e.vertices:
                loc = v.dxf.location
                pts.append(P(loc[0], loc[1]))
        # 闭合多段线：CAD 靠 closed 标志隐含最后一段（末点→首点），
        # 不存为顶点。这里显式补上闭合点，否则闭合矩形会被识别成开放链。
        if closed_flag and pts and math.dist(pts[0], pts[-1]) > 1e-9:
            pts.append(pts[0])
        return Entity(id=f"E{idx:04d}", etype=etype, layer=layer,
                      points=pts, source_handle=handle)

    # 参考实体：提取几何/文字为渲染子对象列表，供画布"忽略层"显示
    return _reference_entity(e, idx, layer, handle, scale)


def _clean_text(s: str) -> str:
    """TEXT/MTEXT 内容轻量清洗：去格式指令码（\\A1; \\f..; \\P 换行、%%c 直径等）。"""
    import re
    s = s.replace("\\P", " ")                 # MTEXT 换行码 → 空格
    s = re.sub(r"\\[A-Za-z][^;\\]*;", "", s)  # \A1; \fSimSun|b0; 等指令码
    s = s.replace("{", "").replace("}", "")  # 分组大括号
    s = re.sub(r"%%[dD]", "°", s)            # 度符号
    s = re.sub(r"%%[cC]", "φ", s)            # 直径符号（常见大写 %%C）
    s = re.sub(r"%%[pP]", "±", s)            # 正负公差
    return s.strip()


def _primitive_render(v, scale: float) -> list[dict]:
    """DXF 基元（virtual_entities 展开产物）→ 渲染子对象列表。

    子对象结构（仅画布显示用）：
      {"kind": "polyline", "pts": [(x,y), ...]}
      {"kind": "circle",   "circle": (cx, cy, d)}
      {"kind": "text",     "pos": (x, y), "text": str}
      {"kind": "point",    "pos": (x, y)}
    """
    t = v.dxftype()

    def P(x, y):
        return (x * scale, y * scale)

    if t == "LINE":
        s, e2 = v.dxf.start, v.dxf.end
        return [{"kind": "polyline", "pts": [P(s[0], s[1]), P(e2[0], e2[1])]}]
    if t == "ARC":
        from geom import discretize_arc
        c = v.dxf.center
        r = float(v.dxf.radius) * scale
        a0 = math.radians(float(v.dxf.start_angle))
        a1 = math.radians(float(v.dxf.end_angle))
        if a1 <= a0:
            a1 += 2 * math.pi
        pts = discretize_arc(c[0] * scale, c[1] * scale, r, a0, a1)
        return [{"kind": "polyline", "pts": pts}] if len(pts) >= 2 else []
    if t == "CIRCLE":
        c = v.dxf.center
        d = float(v.dxf.radius) * 2 * scale
        return [{"kind": "circle", "circle": (c[0] * scale, c[1] * scale, d)}]
    if t in ("LWPOLYLINE", "POLYLINE"):
        if t == "LWPOLYLINE":
            pts = [P(x, y) for x, y in v.get_points("xy")]
        else:
            pts = [P(p.dxf.location[0], p.dxf.location[1]) for p in v.vertices]
        return [{"kind": "polyline", "pts": pts}] if len(pts) >= 2 else []
    if t == "TEXT":
        ip = v.dxf.insert
        return [{"kind": "text", "pos": P(ip[0], ip[1]),
                 "text": _clean_text(str(v.dxf.text))}]
    if t == "MTEXT":
        ip = v.dxf.insert
        return [{"kind": "text", "pos": P(ip[0], ip[1]),
                 "text": _clean_text(getattr(v, "text", ""))}]
    if t == "SOLID":  # 实心箭头/填充：vtx0~vtx3 闭合折线
        pts = [P(p[0], p[1]) for p in (v.dxf.vtx0, v.dxf.vtx1,
                                        v.dxf.vtx2, v.dxf.vtx3)]
        if math.dist(pts[0], pts[-1]) > 1e-9:
            pts.append(pts[0])
        return [{"kind": "polyline", "pts": pts}]
    if t in ("SPLINE", "ELLIPSE"):
        pts = [P(p[0], p[1]) for p in v.flattening(0.2 / scale)]
        return [{"kind": "polyline", "pts": pts}] if len(pts) >= 2 else []
    if t == "POINT":
        loc = v.dxf.location
        return [{"kind": "point", "pos": P(loc[0], loc[1])}]
    return []


def _reference_entity(e, idx: int, layer: str, handle: str,
                      scale: float) -> Entity:
    """参考实体（DIMENSION/TEXT/MTEXT/HATCH 等忽略层实体）→ Entity。

    几何/文字提取为渲染子对象列表，附加在动态属性 render 上（不改
    model.Entity 字段定义）；points 保持空列表，不参与拼环/特征识别。
    导入时一次性展开（天然缓存），redraw 只遍历结果。
    """
    etype = e.dxftype()
    ent = Entity(id=f"E{idx:04d}", etype=etype, layer=layer,
                 points=[], source_handle=handle)
    objs: list[dict] = []

    def P(x, y):
        return (x * scale, y * scale)

    try:
        if etype in ("DIMENSION", "INSERT"):
            # 标注/块引用：展开成渲染基元（尺寸线/延伸线/箭头/标注文字）
            for v in e.virtual_entities():
                objs += _primitive_render(v, scale)
        elif etype == "HATCH":
            # 剖面线：ezdxf 官方转换器整体离散边界（支持线/弧/椭圆/样条边）。
            # EdgePath 无 virtual_entities()，elevation 需 float 而非 Vec3。
            from ezdxf.path import from_hatch_boundary_path
            ocs = e.ocs()
            elev = float(e.dxf.elevation.z)
            for path in e.paths:
                try:
                    bp = from_hatch_boundary_path(path, ocs, elev)
                    pts = [(v[0] * scale, v[1] * scale)
                           for v in bp.flattening(0.2 / scale)]
                    if len(pts) >= 2:
                        objs.append({"kind": "polyline", "pts": pts})
                except Exception:  # noqa: BLE001 —— 单条边界失败不影响其余
                    pass
        elif etype == "TEXT":
            ip = e.dxf.insert
            objs.append({"kind": "text", "pos": P(ip[0], ip[1]),
                         "text": _clean_text(str(e.dxf.text))})
        elif etype == "MTEXT":
            ip = e.dxf.insert
            objs.append({"kind": "text", "pos": P(ip[0], ip[1]),
                         "text": _clean_text(getattr(e, "text", ""))})
        elif etype in ("SPLINE", "ELLIPSE"):
            pts = [P(p[0], p[1]) for p in e.flattening(0.2 / scale)]
            if len(pts) >= 2:
                objs.append({"kind": "polyline", "pts": pts})
        elif etype == "LEADER":
            pts = [P(p[0], p[1]) for p in e.dxf.vertices]
            if len(pts) >= 2:
                objs.append({"kind": "polyline", "pts": pts})
        elif etype == "POINT":
            loc = e.dxf.location
            objs.append({"kind": "point", "pos": P(loc[0], loc[1])})
        elif etype == "SOLID":
            pts = [P(p[0], p[1]) for p in (e.dxf.vtx0, e.dxf.vtx1,
                                           e.dxf.vtx2, e.dxf.vtx3)]
            if math.dist(pts[0], pts[-1]) > 1e-9:
                pts.append(pts[0])
            if len(pts) >= 3:
                objs.append({"kind": "polyline", "pts": pts})
    except Exception:  # noqa: BLE001 —— 残缺实体跳过显示，不阻断导入
        pass

    ent.render = objs  # type: ignore[attr-defined]  # 动态附加，画布显示用
    return ent


def _bulge_arc(p1: tuple[float, float], p2: tuple[float, float], bulge: float,
               scale: float = 1.0) -> list[tuple[float, float]]:
    """LWPOLYLINE bulge 圆弧离散（不含 p1，含 p2）。bulge=tan(Δ角/4)。"""
    from geom import discretize_arc
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    chord = math.hypot(dx, dy)
    if chord < 1e-12:
        return []
    theta = 4.0 * math.atan(bulge)  # 包含角（有符号）
    r = chord / (2.0 * abs(math.sin(theta / 2)))
    # 圆心在弦中垂线上
    mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
    h = math.sqrt(max(r * r - (chord / 2) ** 2, 0))
    # 方向：bulge>0 逆时针（凸向左）
    nx, ny = -dy / chord, dx / chord
    sign = 1.0 if bulge > 0 else -1.0
    cx = mx + nx * h * sign
    cy = my + ny * h * sign
    a0 = math.atan2(p1[1] - cy, p1[0] - cx)
    a1 = a0 + theta
    arc = discretize_arc(cx, cy, r, a0, a1)
    return arc[1:] if arc else []
