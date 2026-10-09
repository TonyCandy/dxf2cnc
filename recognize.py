# -*- coding: utf-8 -*-
"""L3：特征识别。确定性规则 R1–R9（见《项目需求总结更新》§4），
结合图层角色提示与 rules.json 标准孔表。识别结果用户可在 GUI 改。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from model import (Contour, Feature, FeatureParams, JobConfig,
                   THROUGH_HOLE, COUNTERBORE, DRILL, CIRCULAR_POCKET,
                   RECT_POCKET, POCKET, ON_VECTOR, OUTLINE, UNKNOWN,
                   SPIRAL, OFFSET_POCKET, CONTOUR)
import geom
from io_dxf import (DxfData, ROLE_DRILL, ROLE_TOOL, ROLE_SECTION,
                    ROLE_ZERO, ROLE_GEOM, ROLE_ISLAND,
                    layer_diameter_hint)


# ----------------------------------------------------------------------------
# 规则配置（rules.json）
# ----------------------------------------------------------------------------

@dataclass
class HoleRule:
    diameter: float
    kind: str = THROUGH_HOLE
    tool: str = "T1"
    depth: float = 25.0
    n_passes: int | None = 3
    stepdown: float = 0.0
    strategy: str = SPIRAL
    stepover_factor: float = 0.9            # 行距系数（×刀径），偏置/平行清槽
    stepover_mm: float | None = None        # 绝对行距 mm；非 None 时优先于系数
    parallel_axis: str = "X"                # 平行清槽走刀方向：X / Y
    pocket_direction: str = "outside_in"    # 偏置清槽起刀：outside_in 外→内 / inside_out 内→外


@dataclass
class LayerDefault:
    depth: float = 0.0
    n_passes: int | None = None
    strategy: str = OFFSET_POCKET
    tool: str = "T1"
    stepover_factor: float = 0.9
    stepover_mm: float | None = None
    parallel_axis: str = "X"
    pocket_direction: str = "outside_in"


@dataclass
class Rules:
    standard_holes: list[HoleRule] = field(default_factory=lambda: [
        HoleRule(11.0, THROUGH_HOLE, "T1", 25.0, 3),
        HoleRule(23.0, COUNTERBORE, "T1", 4.0, 1),
        HoleRule(5.5, DRILL, "T2", 15.0, stepdown=5.0),
    ])
    layer_defaults: dict[str, LayerDefault] = field(default_factory=lambda: {
        "T01": LayerDefault(depth=3.0, n_passes=1, tool="T1"),
        "T02": LayerDefault(depth=10.0, n_passes=2, tool="T1"),
    })

    @staticmethod
    def load(path: str | Path) -> "Rules":
        p = Path(path)
        if not p.exists():
            return Rules()
        data = json.loads(p.read_text(encoding="utf-8"))
        holes = [HoleRule(**h) for h in data.get("standard_holes", [])]
        layers = {k: LayerDefault(**v) for k, v in data.get("layer_defaults", {}).items()}
        return Rules(standard_holes=holes or Rules().standard_holes, layer_defaults=layers)

    def match_hole(self, d: float, snap: float = 0.15) -> HoleRule | None:
        best = None
        best_dd = snap
        for h in self.standard_holes:
            dd = abs(h.diameter - d)
            if dd <= best_dd:
                best, best_dd = h, dd
        return best


# ----------------------------------------------------------------------------
# 识别结果
# ----------------------------------------------------------------------------

@dataclass
class RecognitionResult:
    contours: list[Contour] = field(default_factory=list)
    features: list[Feature] = field(default_factory=list)
    stock: Contour | None = None
    warnings: list[str] = field(default_factory=list)


def snap_diameter(d: float, standard: list[float], tol: float) -> float:
    best = d
    best_dd = tol
    for s in standard:
        dd = abs(s - d)
        if dd <= best_dd:
            best, best_dd = s, dd
    return best


def recognize(data: DxfData, cfg: JobConfig | None = None,
              rules: Rules | None = None) -> RecognitionResult:
    """L1 → L2 → L3 主流程。"""
    cfg = cfg or JobConfig()
    rules = rules or Rules()
    res = RecognitionResult()

    # L2
    contours = geom.build_contours(data.entities, cfg)
    stock = geom.mark_stock_outline(contours)
    res.contours = contours
    res.stock = stock
    if stock is not None and stock.is_rect and stock.size:
        res.warnings.append(
            f"已识别外框 {stock.size[0]:.0f}×{stock.size[1]:.0f}（默认不加工）")

    by_id = {c.id: c for c in contours}

    # 避让岛图层打标：口袋清料时避让、不生成独立特征（岛上孔等由其它图层设定）
    for c in contours:
        if data.layer_roles.get(c.layer) == ROLE_ISLAND:
            c.is_island = True

    # ---- 同心分组（并查集） ----
    circles = [c for c in contours if c.closed and c.is_circle and c.area > 0]
    loc_dsu = geom._DSU()
    for i, a in enumerate(circles):
        for b in circles[i + 1:]:
            if math.dist(a.centroid, b.centroid) <= cfg.eps_concentric:
                loc_dsu.union(id(a), id(b))
    # 槽包含孔 → 同站
    pockets = [c for c in contours if c.closed and not c.is_circle
               and not c.is_stock_outline and c.area > 0]
    for p in pockets:
        for c in circles:
            if geom.point_in_ring(c.centroid, p.pts):
                loc_dsu.union(id(p), id(c))

    def loc_of(c: Contour) -> str:
        return f"L{loc_dsu.find(id(c)) % 100000:05d}"

    # ---- 逐环分类 ----
    feat_n = 0

    def new_feature(kind: str, c: Contour, params: FeatureParams,
                    label: str, group: str) -> Feature:
        nonlocal feat_n
        f = Feature(id=f"F{feat_n:03d}", kind=kind, contour_id=c.id,
                    label=label, group_key=group, location_id=loc_of(c),
                    params=params)
        feat_n += 1
        res.features.append(f)
        return f

    std = [h.diameter for h in rules.standard_holes] + cfg.standard_holes

    for c in contours:
        if c.is_island:
            # 避让岛：不生成独立加工特征（口袋清料时作为岛避让）
            continue
        if not c.closed:
            # R7 开放链
            f = new_feature(ON_VECTOR, c, FeatureParams(strategy="on_vector"),
                            f"开放链 {len(c.pts)}点", "开放链")
            f.warnings.append("开放轮廓：需手动确认沿矢量加工")
            continue

        if c.is_stock_outline:
            # R8 外框
            f = new_feature(OUTLINE, c, FeatureParams(strategy=CONTOUR),
                            "外框（默认跳过）", "外框")
            f.params.enabled = False
            continue

        role = data.layer_roles.get(c.layer, ROLE_GEOM)

        if c.is_circle and c.diameter:
            d_raw = c.diameter
            d = snap_diameter(d_raw, std, cfg.diameter_snap)
            rule = rules.match_hole(d, cfg.diameter_snap)

            # 图层提示：φ11/φ23 强制直径
            d_hint = layer_diameter_hint(c.layer)
            if d_hint is not None:
                d = snap_diameter(d_hint, std, cfg.diameter_snap) \
                    if any(abs(s - d_hint) <= cfg.diameter_snap for s in std) else d_hint
                rule = rules.match_hole(d, cfg.diameter_snap)

            if rule is not None and rule.kind == THROUGH_HOLE:
                # R1 通孔
                p = FeatureParams(tool_id=rule.tool, depth=rule.depth,
                                  n_passes=rule.n_passes, stepdown=rule.stepdown,
                                  strategy=rule.strategy,
                                  stepover_factor=rule.stepover_factor,
                                  stepover_mm=rule.stepover_mm,
                                  parallel_axis=rule.parallel_axis,
                                  pocket_direction=rule.pocket_direction)
                new_feature(THROUGH_HOLE, c, p,
                            f"φ{d:g} @ ({c.centroid[0]:.1f}, {c.centroid[1]:.1f})",
                            f"φ{d:g} 通孔")
            elif rule is not None and rule.kind == COUNTERBORE:
                # R2 候选：等同心配对后再定（先记为 COUNTERBORE）
                p = FeatureParams(tool_id=rule.tool, depth=rule.depth,
                                  n_passes=rule.n_passes, stepdown=rule.stepdown,
                                  strategy=rule.strategy,
                                  stepover_factor=rule.stepover_factor,
                                  stepover_mm=rule.stepover_mm,
                                  parallel_axis=rule.parallel_axis,
                                  pocket_direction=rule.pocket_direction)
                new_feature(COUNTERBORE, c, p,
                            f"φ{d:g} 沉孔 @ ({c.centroid[0]:.1f}, {c.centroid[1]:.1f})",
                            f"φ{d:g} 沉孔")
            elif rule is not None and rule.kind == DRILL:
                # R3 钻孔
                p = FeatureParams(tool_id=rule.tool, depth=rule.depth,
                                  n_passes=rule.n_passes, stepdown=rule.stepdown,
                                  strategy=rule.strategy,
                                  stepover_factor=rule.stepover_factor,
                                  stepover_mm=rule.stepover_mm,
                                  parallel_axis=rule.parallel_axis,
                                  pocket_direction=rule.pocket_direction)
                new_feature(DRILL, c, p,
                            f"φ{d:g} 钻孔 @ ({c.centroid[0]:.1f}, {c.centroid[1]:.1f})",
                            f"φ{d:g} 钻孔")
            else:
                # 图层角色提示
                if role == ROLE_SECTION:
                    p = FeatureParams(strategy=SPIRAL)
                    f = new_feature(DRILL, c, p, f"φ{d_raw:.1f} 剖面孔",
                                    "剖面钻孔")
                    f.warnings.append("剖面线层圆：请确认刀号与深度")
                elif role == ROLE_DRILL:
                    p = FeatureParams(strategy=SPIRAL)
                    f = new_feature(THROUGH_HOLE, c, p, f"φ{d_raw:.1f} 孔",
                                    f"φ{d_raw:.1f} 孔")
                    f.warnings.append(f"φ{d_raw:.1f} 非标准孔径：请确认参数")
                else:
                    # R4 圆形凹槽
                    p = FeatureParams(strategy=SPIRAL)
                    f = new_feature(CIRCULAR_POCKET, c, p,
                                    f"φ{d_raw:.1f} 圆槽 @ ({c.centroid[0]:.1f}, {c.centroid[1]:.1f})",
                                    f"φ{d_raw:.1f} 圆槽")
                    f.warnings.append("圆槽：深度需手填")
            continue

        # 矩形 / 一般闭环
        if c.is_rect and c.size:
            w, h = c.size
            # 图层刀号默认（T01/T02）
            # 图层默认：精确按图层名匹配 rules.json layer_defaults
            ld = rules.layer_defaults.get(c.layer)
            if ld is not None:
                p = FeatureParams(tool_id=ld.tool, depth=ld.depth,
                                  n_passes=ld.n_passes, stepdown=0,
                                  strategy=ld.strategy,
                                  stepover_factor=ld.stepover_factor,
                                  stepover_mm=ld.stepover_mm,
                                  parallel_axis=ld.parallel_axis,
                                  pocket_direction=ld.pocket_direction)
            else:
                p = FeatureParams(strategy=OFFSET_POCKET)
            r_txt = f" R{c.corner_r:g}" if c.corner_r else ""
            f = new_feature(RECT_POCKET, c, p,
                            f"{w:g}×{h:g}{r_txt} @ ({c.centroid[0]:.1f}, {c.centroid[1]:.1f})",
                            f"{w:g}×{h:g} 沉槽")
            if ld is None:
                f.warnings.append("矩形沉槽：深度需手填")
        else:
            # R6 一般凹槽
            p = FeatureParams(strategy=OFFSET_POCKET)
            f = new_feature(POCKET, c, p,
                            f"凹槽 {c.area:.0f}mm² @ ({c.centroid[0]:.1f}, {c.centroid[1]:.1f})",
                            "一般凹槽")
            f.warnings.append("一般凹槽：深度需手填")

    # ---- 同心配对：THROUGH_HOLE + 同心 COUNTERBORE → paired ----
    holes = [f for f in res.features if f.kind == THROUGH_HOLE]
    bores = [f for f in res.features if f.kind == COUNTERBORE]
    for f in holes:
        c1 = by_id[f.contour_id]
        for g in bores:
            c2 = by_id[g.contour_id]
            if (c2.diameter or 0) > (c1.diameter or 0) and \
               math.dist(c1.centroid, c2.centroid) <= cfg.eps_concentric:
                f.paired_ids.append(g.id)
                g.paired_ids.append(f.id)

    # 无同伴的 COUNTERBORE 独立存在时保持原类型（浅孔）
    res.warnings.extend(data.warnings)
    return res
