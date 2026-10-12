# -*- coding: utf-8 -*-
"""排序：区域内固定优先级 + 区域间绕板启发式（对齐领跑黄金顺序）。

§8.1 同一站（location）：通孔/钻孔 → 沉孔 → 沉槽 → 轮廓。
§8.2 绕板：从离原点最近站开始，左列向上 → 顶边向右 → 右列向下（顺时针绕形心），
其余站就近插入。
"""
from __future__ import annotations

import math

from model import (Feature, Contour, THROUGH_HOLE, DRILL, COUNTERBORE,
                   RECT_POCKET, POCKET, CIRCULAR_POCKET, ON_VECTOR, OUTLINE,
                   CONTOUR, OFFSET_POCKET, PARALLEL_POCKET)

# 区域内优先级（越小越先）
KIND_PRIORITY = {
    THROUGH_HOLE: 0, DRILL: 0,
    COUNTERBORE: 1,
    RECT_POCKET: 2, POCKET: 2, CIRCULAR_POCKET: 2,
    ON_VECTOR: 3, OUTLINE: 4,
}

# 贪心聚类：簇内类序（沉孔 → 矩槽/凹槽 → 圆槽 → 通孔/钻孔 → 矢量）
CLUSTER_KIND_PRIORITY = {
    COUNTERBORE: 0,
    RECT_POCKET: 1, POCKET: 1,       # 矩槽/凹槽 → 第二优先
    CIRCULAR_POCKET: 2,               # 圆槽 → 第三优先
    THROUGH_HOLE: 3, DRILL: 3,        # 通孔/钻孔 → 第四优先
    ON_VECTOR: 4,                     # 矢量 → 最后
}
CLUSTER_DIST = 80.0          # 站间聚类距离阈值 mm（可调）


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
    cluster: 站贪心聚类 → 簇间就近 → 簇内沉孔→矩槽/凹槽→圆槽→通孔/钻孔→矢量 → 外轮廓最后
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

    if strategy == "cluster":
        return _sort_cluster(enabled, disabled, by_id)

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


def _cluster_stations(stations: list[list[Feature]], anchor_of,
                     dist: float = CLUSTER_DIST) -> list[list[int]]:
    """贪心聚类（递归扩散）：任取未归簇站作种子，把与簇内任一站锚点距离
    ≤ dist 的站全部并入，直到无新并入；重复直到全部分组，孤立站单独成簇。
    返回簇列表，每簇是站下标列表（按加入顺序）。"""
    unassigned = set(range(len(stations)))
    clusters: list[list[int]] = []
    while unassigned:
        seed = next(iter(unassigned))
        unassigned.discard(seed)
        cluster = [seed]
        anchors = [anchor_of(stations[seed])]
        changed = True
        while changed:
            changed = False
            for i in list(unassigned):
                a = anchor_of(stations[i])
                if any(math.dist(a, p) <= dist for p in anchors):
                    cluster.append(i)
                    anchors.append(a)
                    unassigned.discard(i)
                    changed = True
        clusters.append(cluster)
    return clusters


def _sort_cluster(enabled: list[Feature], disabled: list[Feature],
                  by_id: dict[str, Contour]) -> list[Feature]:
    """贪心聚类排序（多板排版）：站聚成区域簇 → 簇间从原点最近邻 →
    簇内按类序（沉孔 → 通孔/钻孔 → 槽）桶内最近邻 → 外轮廓
    （params.strategy==CONTOUR）最后切割。"""
    if not enabled:
        result = list(disabled)
        for i, f in enumerate(result):
            f.order = i + 1
        return result

    # 外轮廓最后：按 strategy 判定（轮廓外图层 kind=POCKET 但策略为 contour）
    contour_feats = [f for f in enabled if f.params.strategy == CONTOUR]
    inner = [f for f in enabled if f.params.strategy != CONTOUR]

    # inner 按站分组
    stations: dict[str, list[Feature]] = {}
    for f in inner:
        stations.setdefault(f.location_id, []).append(f)
    station_list = list(stations.values())

    def st_anchor(fs: list[Feature]) -> tuple[float, float]:
        return _feature_anchor(fs[0], by_id)

    clusters = _cluster_stations(station_list, st_anchor)

    # 簇锚点：簇内所有站的锚点坐标均值
    def cl_anchor(idx: list[int]) -> tuple[float, float]:
        pts = [st_anchor(station_list[i]) for i in idx]
        return (sum(p[0] for p in pts) / len(pts),
                sum(p[1] for p in pts) / len(pts))

    ordered: list[Feature] = []
    cur = (0.0, 0.0)

    # 簇间最近邻：从原点出发，每次去最近的未访问簇
    remaining = list(range(len(clusters)))
    while remaining:
        ci = min(remaining, key=lambda i: math.dist(cur, cl_anchor(clusters[i])))
        remaining.remove(ci)
        # 簇内配对式循环：取最近的未做清槽 → 做它 → 立即做离它最近的
        # 未做非清槽（"孔"，形心配对、组内清槽先）；无清槽时按最近邻取任意
        # （内层用 todo，勿与外层簇循环的 remaining 重名——重名会提前终止簇遍历）
        feats = [f for i in clusters[ci] for f in station_list[i]]
        todo = list(feats)
        while todo:
            clears = [f for f in todo
                      if f.params.strategy in (OFFSET_POCKET, PARALLEL_POCKET)]
            pool = clears if clears else todo
            ni = min(range(len(pool)),
                     key=lambda j: math.dist(cur, _feature_anchor(pool[j], by_id)))
            f = pool[ni]
            todo.remove(f)
            ordered.append(f)
            cur = _feature_anchor(f, by_id)
            # 配对：立即做离刚做完特征最近的未做非清槽（"孔"）
            if clears and todo:
                others = [g for g in todo
                          if g.params.strategy not in (OFFSET_POCKET, PARALLEL_POCKET)]
                if others:
                    nj = min(range(len(others)),
                             key=lambda j: math.dist(cur, _feature_anchor(others[j], by_id)))
                    g = others[nj]
                    todo.remove(g)
                    ordered.append(g)
                    cur = _feature_anchor(g, by_id)

    # 外轮廓最后：从当前位置最近邻连接
    while contour_feats:
        ni = min(range(len(contour_feats)),
                 key=lambda j: math.dist(cur, _feature_anchor(contour_feats[j], by_id)))
        ordered.append(contour_feats.pop(ni))

    result = ordered + disabled
    for i, f in enumerate(result):
        f.order = i + 1
    return result
