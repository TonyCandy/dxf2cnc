# -*- coding: utf-8 -*-
"""排序：区域内固定优先级 + 区域间绕板启发式（对齐领跑黄金顺序）。

§8.1 同一站（location）：通孔/钻孔 → 沉孔 → 沉槽 → 轮廓。
§8.2 绕板：从离原点最近站开始，左列向上 → 顶边向右 → 右列向下（顺时针绕形心），
其余站就近插入。
"""
from __future__ import annotations

import math

from model import (Feature, Contour, THROUGH_HOLE, DRILL, COUNTERBORE,
                   RECT_POCKET, POCKET, CIRCULAR_POCKET, ON_VECTOR, OUTLINE)

# 区域内优先级（越小越先）
KIND_PRIORITY = {
    THROUGH_HOLE: 0, DRILL: 0,
    COUNTERBORE: 1,
    RECT_POCKET: 2, POCKET: 2, CIRCULAR_POCKET: 2,
    ON_VECTOR: 3, OUTLINE: 4,
}


def _feature_anchor(f: Feature, contours_by_id: dict[str, Contour]) -> tuple[float, float]:
    c = contours_by_id.get(f.contour_id)
    return c.centroid if c else (0.0, 0.0)


def sort_features(features: list[Feature], contours: list[Contour],
                  strategy: str = "around") -> list[Feature]:
    """返回排序后的新列表，并写 order。strategy: around | by_tool | nearest | manual。

    around: 站内按类型优先级；站间绕板（默认，与领跑一致）
    by_tool: 先所有 T1 再 T2（站内仍先孔后槽）
    nearest: 最近邻
    manual: 完全按现有 order 保持（用户手动调整后锁定）
    """
    by_id = {c.id: c for c in contours}
    enabled = [f for f in features if f.params.enabled]
    disabled = [f for f in features if not f.params.enabled]

    if strategy == "manual":
        enabled.sort(key=lambda f: (f.order or 999, f.id))
        result = enabled + disabled
        for i, f in enumerate(result):
            f.order = i + 1
        return result

    # 按 location 分站
    stations: dict[str, list[Feature]] = {}
    for f in enabled:
        stations.setdefault(f.location_id, []).append(f)

    def sort_in_station(fs: list[Feature]) -> list[Feature]:
        return sorted(fs, key=lambda f: (KIND_PRIORITY.get(f.kind, 9), f.id))

    station_list = [sort_in_station(fs) for fs in stations.values()]
    if not station_list:
        for i, f in enumerate(disabled):
            f.order = i + 1
        return disabled

    def station_anchor(fs: list[Feature]) -> tuple[float, float]:
        return _feature_anchor(fs[0], by_id)

    if strategy == "by_tool":
        # 先按刀号组，组内绕板
        groups: dict[str, list[list[Feature]]] = {}
        for st in station_list:
            tset = {f.params.tool_id for f in st}
            key = sorted(tset)[0] if len(tset) == 1 else "MIX"
            groups.setdefault(key, []).append(st)
        ordered: list[list[Feature]] = []
        for key in sorted(groups):
            ordered.extend(_order_stations(groups[key], station_anchor, "around"))
    else:
        ordered = _order_stations(station_list, station_anchor, strategy)

    result = [f for st in ordered for f in st] + disabled
    for i, f in enumerate(result):
        f.order = i + 1
    return result


def _order_stations(stations: list[list[Feature]],
                    anchor_of, strategy: str) -> list[list[Feature]]:
    if len(stations) <= 1 or strategy == "nearest":
        return _nearest_order(stations, anchor_of)

    # 绕板：以全部站的形心为中心，从离机床原点最近的站开始顺时针绕行
    # （左列↑ → 顶边→ → 右列↓，对齐领跑黄金顺序）
    pts = [anchor_of(st) for st in stations]
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)

    # 起始站：离原点最近
    start_i = min(range(len(pts)), key=lambda i: math.hypot(pts[i][0], pts[i][1]))
    sa = math.atan2(pts[start_i][1] - cy, pts[start_i][0] - cx)

    def angle_key(i: int) -> float:
        """从起始站起算的顺时针角距（0°=起始站，递增为顺时针绕行）。"""
        a = math.atan2(pts[i][1] - cy, pts[i][0] - cx)
        return (sa - a) % (2.0 * math.pi)

    idx = sorted(range(len(stations)), key=angle_key)
    return [stations[i] for i in idx]


def _nearest_order(stations, anchor_of) -> list[list[Feature]]:
    remaining = list(range(len(stations)))
    cur = (0.0, 0.0)
    ordered: list[list[Feature]] = []
    while remaining:
        i = min(remaining, key=lambda i: math.dist(cur, anchor_of(stations[i])))
        ordered.append(stations[i])
        cur = anchor_of(stations[i])
        remaining.remove(i)
    return ordered
