# -*- coding: utf-8 -*-
"""黄金样例回归测试 P1–P11（《项目需求总结更新》§11）。

运行：python -m pytest tests/ -v
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cam import CamJob                                    # noqa: E402
from model import (JobConfig, Feature, FeatureParams, THROUGH_HOLE,  # noqa: E402
                   COUNTERBORE, DRILL, RECT_POCKET, OUTLINE, UNKNOWN,
                   SPIRAL, PARALLEL_POCKET, OFFSET_POCKET, Entity,
                   RAPID, FEED, PLUNGE, RETRACT)
from sort import sort_features                            # noqa: E402
from model import Tool as ModelTool                       # noqa: E402
from io_dxf import DxfData, ROLE_ZERO, ROLE_GEOM, ROLE_ISLAND, ROLE_DRILL  # noqa: E402
from recognize import Rules, recognize                    # noqa: E402


def GOLDEN_CFG() -> JobConfig:
    """黄金样例参数（历史默认：孔过切 0.15 / 槽过切 0.4）。

    产品默认已改为 0（用户用"刀径改小"法自实现过切），
    黄金对比测试仍按样例生成时的参数验证。
    """
    return JobConfig(hole_overcut=0.15, pocket_overcut=0.4)
import post_baoyuan                                       # noqa: E402
import toolpath                                           # noqa: E402
from cnc_compare import parse_cnc, compare                # noqa: E402

DATA = ROOT / "基础数据文件"          # 基础数据归档目录
DXF = DATA / "领跑1.dxf"
DXF2 = DATA / "领跑2.dxf"
DXF_HANDLE = DATA / "地板开孔示意-把手开孔.dxf"
GOLD1 = DATA / "领跑1.cnc"
GOLD2 = DATA / "领跑2.cnc"
OUT = ROOT / "tests" / "out"


@pytest.fixture()
def job1() -> CamJob:
    job = CamJob(GOLDEN_CFG())
    job.load(DXF)
    job.sort()
    return job


# P1 同心识别 ---------------------------------------------------------------

def test_p1_concentric(job1: CamJob):
    fs = job1.result.features
    holes = [f for f in fs if f.kind == THROUGH_HOLE]
    bores = [f for f in fs if f.kind == COUNTERBORE]
    assert len(holes) == 6 and len(bores) == 6
    # 每组同心
    by_id = {c.id: c for c in job1.result.contours}
    for h in holes:
        assert h.paired_ids, f"{h.label} 未配对沉孔"
    outs = [f for f in fs if f.kind == OUTLINE]
    assert len(outs) == 1 and not outs[0].params.enabled


# P2 图层0 内槽（领跑2） ------------------------------------------------------

def test_p2_inner_rect_pocket():
    job = CamJob(GOLDEN_CFG())
    res = job.load(DXF2)
    holes = [f for f in res.features if f.kind == THROUGH_HOLE]
    pockets = [f for f in res.features if f.kind == RECT_POCKET]
    outlines = [f for f in res.features if f.kind == OUTLINE]
    assert len(holes) == 4
    assert len(pockets) == 1
    assert len(outlines) == 1 and not outlines[0].params.enabled
    # 口袋 110×160
    by_id = {c.id: c for c in res.contours}
    p = pockets[0]
    c = by_id[p.contour_id]
    assert c.size and abs(c.size[0] - 110) < 1 and abs(c.size[1] - 160) < 1


# P3 图层名缺失（把手图：圆在粗实线层） ---------------------------------------

def test_p3_layername_fallback():
    job = CamJob(GOLDEN_CFG())
    res = job.load(DXF_HANDLE)
    holes = [f for f in res.features if f.kind == THROUGH_HOLE]
    bores = [f for f in res.features if f.kind == COUNTERBORE]
    assert len(holes) == 4, "粗实线层上的 φ11 应兜底识别为通孔"
    assert len(bores) == 4, "φ23 应识别为沉孔"
    # 把手沉槽（圆角矩形 96×60）
    pockets = [f for f in res.features if f.kind == RECT_POCKET]
    assert len(pockets) >= 1


# P4 参考层过滤 --------------------------------------------------------------

def test_p4_reference_filtered(job1: CamJob):
    # 加工实体不含尺寸线层
    layers = {e.layer for e in job1.data.entities}
    assert not any("尺寸" in l for l in layers)
    # DIMENSION 不参与
    assert all(e.etype != "DIMENSION" for e in job1.data.entities)


# P5 绕板排序 ---------------------------------------------------------------

def test_p5_around_order(job1: CamJob):
    expected = [(20.0, 20.0), (20.0, 535.43), (20.0, 1050.86),
                (498.0, 1050.86), (498.0, 535.43), (498.0, 20.0)]
    by_id = {c.id: c for c in job1.result.contours}
    seq = []
    for f in job1.ordered:
        if f.kind != THROUGH_HOLE or not f.params.enabled:
            continue
        c = by_id[f.contour_id]
        seq.append((round(c.centroid[0], 1), round(c.centroid[1], 1)))
    assert len(seq) == 6
    for (ex, ey), (gx, gy) in zip(expected, seq):
        assert abs(ex - gx) < 1.0 and abs(ey - gy) < 1.0, f"{seq}"


# P6/P7 螺旋半径与分刀 -------------------------------------------------------

def test_p6_spiral_radius(job1: CamJob):
    tool = job1.lib.get("T1")
    mv = toolpath.helical_hole(20.0, 20.0, 11.0, tool, [25.0], job1.cfg)
    first = next(m for m in mv if m.kind == RAPID)
    r = abs(first.x - 20.0)
    assert abs(r - 2.6496) < 0.02, f"R={r}"


def test_p7_pass_depths():
    from model import FeatureParams
    p = FeatureParams(depth=25.0, n_passes=3)
    assert p.pass_depths() == [8.3333, 16.6667, 25.0]


# P8 区域内顺序（每角先通孔全部层再沉孔） ------------------------------------

def test_p8_intra_location_order(job1: CamJob):
    prev: dict[str, list[str]] = {}
    for f in job1.ordered:
        kinds = prev.setdefault(f.location_id, [])
        if f.kind == COUNTERBORE and THROUGH_HOLE in kinds:
            continue  # 正常：孔后沉孔
        kinds.append(f.kind)
    for loc, kinds in prev.items():
        # 无沉孔在通孔之前
        first_hole = kinds.index(THROUGH_HOLE) if THROUGH_HOLE in kinds else 999
        first_bore = kinds.index(COUNTERBORE) if COUNTERBORE in kinds else 999
        assert first_hole < first_bore or first_bore == 999


# P9 口袋偏置（刀心框 104.8×154.8） -----------------------------------------

def test_p9_pocket_offset():
    job = CamJob(GOLDEN_CFG())
    job.load(DXF2)
    # 找到 110×160 槽
    pocket = next(f for f in job.result.features if f.kind == RECT_POCKET)
    c = next(c for c in job.result.contours if c.id == pocket.contour_id)
    tool = job.lib.get("T1")
    mv = toolpath.pocket_moves(c.pts, [], tool, [10.0], job.cfg)
    xs = [m.x for m in mv if m.x is not None]
    ys = [m.y for m in mv if m.y is not None]
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    assert abs(w - 104.8) < 0.05, f"刀心宽 {w}"
    assert abs(h - 154.8) < 0.05, f"刀心高 {h}"


# P10 后处理文本格式 ---------------------------------------------------------

def test_p10_post_format(job1: CamJob, tmp_path):
    written = job1.export_by_tool(tmp_path, "t")
    assert "T1" in written
    text = Path(written["T1"]).read_bytes().decode("ascii")
    assert text.startswith("%\r\n \r\n")
    assert text.endswith("M30\r\n%\r\n")
    assert "G02" not in text and "G03" not in text
    # N 连续
    ns = [int(m) for m in re.findall(r"^N(\d+)", text, re.M)]
    assert ns[0] == 3
    assert all(b - a == 1 for a, b in zip(ns, ns[1:]))
    # 小数位
    assert re.search(r"G1Z-8\.3333F3000\.0", text)
    assert "S18000" in text
    assert "G0X0.0000Y3000.0000" in text  # home Y3000


# P11 GBK 编码图层名 ---------------------------------------------------------

def test_p11_gbk_layer():
    from io_dxf import read_dxf, ROLE_DRILL
    data = read_dxf(DXF, GOLDEN_CFG())
    assert "φ11" in data.layer_roles and data.layer_roles["φ11"] == ROLE_DRILL
    circles = [e for e in data.entities if e.etype == "CIRCLE" and e.layer == "φ11"]
    assert len(circles) == 6


# P0 全文件拓扑对比（领跑1 / 领跑2） -----------------------------------------

def test_p0_topology_lingpao1(job1: CamJob, tmp_path):
    written = job1.export_by_tool(tmp_path, "t")
    gold = parse_cnc(str(GOLD1))
    gen = parse_cnc(written["T1"])
    matched, total, diffs = compare(gold, gen)
    assert matched == total and not diffs, diffs[:5]


def test_p0_topology_lingpao2(tmp_path):
    job = CamJob(GOLDEN_CFG())
    job.load(DXF2)
    job.sort()
    written = job.export_by_tool(tmp_path, "t")
    gold = parse_cnc(str(GOLD2))
    gen = parse_cnc(written["T1"])
    matched, total, diffs = compare(gold, gen)
    # 领跑2 黄金含 110×160 槽（三刀）+ 4 组孔；口袋特征默认无深度时跳过，
    # 故这里只要求孔环全匹配
    hole_gold = [g for g in gold if g["r"] and g["r"] < 5]
    matched_h, _, _ = compare(hole_gold, [g for g in gen if g["r"] and g["r"] < 5])
    assert matched_h == len(hole_gold), f"孔匹配 {matched_h}/{len(hole_gold)}"


# P12 平行清槽 -----------------------------------------------------------------

def test_p12_parallel_pocket():
    """凸矩形 zigzag：全程直连仅末尾抬刀、行数按行距、奇偶行方向交替。"""
    cfg = GOLDEN_CFG()
    tool = ModelTool(id="T1", diameter=6.0, plunge_f=3000, feed_f=9000)
    ring = [(0, 0), (100, 0), (100, 60), (0, 60)]
    # 可达区 y∈[2.6, 57.4]；行距 3.6 → 2.6,6.2,…,56.6,57.4 共 17 行
    mv = toolpath.parallel_moves(ring, [], tool, [5.0], cfg,
                                 axis="X", stepover=3.6, wall_trim=False)
    retracts = [m for m in mv if m.kind == RETRACT]
    assert len(retracts) == 1, "凸矩形应全程直连，仅末尾抬刀"
    feeds = [m for m in mv if m.kind == FEED]
    ys = sorted({round(m.y, 2) for m in feeds if m.y is not None})
    assert len(ys) == 17, f"行数 {len(ys)}"
    assert abs(ys[0] - 2.6) < 0.05 and abs(ys[-1] - 57.4) < 0.05  # 首末行贴边界
    # 奇偶行方向交替：偶数行首点 x=2.6（左），奇数行首点 x=97.4（右）
    pts = [(m.x, m.y) for m in mv if m.x is not None and m.y is not None]
    first_by_row: dict[float, float] = {}
    for x, y in pts:
        key = round(y, 2)
        if key not in first_by_row:
            first_by_row[key] = round(x, 2)
    ordered_rows = sorted(first_by_row)
    assert first_by_row[ordered_rows[0]] == 2.6      # 第 1 行从左起
    assert first_by_row[ordered_rows[1]] == 97.4     # 第 2 行从右起（zigzag）
    assert first_by_row[ordered_rows[2]] == 2.6


def test_p12b_parallel_island_lift():
    """竖条岛屿把行打断成两段；跨岛衔接必须抬刀。"""
    cfg = GOLDEN_CFG()
    tool = ModelTool(id="T1", diameter=6.0, plunge_f=3000, feed_f=9000)
    ring = [(0, 0), (100, 0), (100, 60), (0, 60)]
    island = [(45, -5), (55, -5), (55, 65), (45, 65)]   # 竖条贯穿
    mv = toolpath.parallel_moves(ring, [island], tool, [5.0], cfg,
                                 axis="X", stepover=5.0, wall_trim=False)
    retracts = [m for m in mv if m.kind == RETRACT]
    assert len(retracts) >= 2, "跨岛衔接应抬刀"
    feeds = [m for m in mv if m.kind == FEED]
    xs = {round(m.x, 2) for m in feeds if m.x is not None}
    # 岛屿区域（45~55）内不应有走刀点
    inside = [x for x in xs if 45.0 < x < 55.0]
    assert not inside, f"刀路穿岛: {inside}"


def test_p12c_parallel_dispatch_and_stepover_mm():
    """gen_feature_moves 分发平行清槽；stepover_mm 绝对行距优先、Y 向走刀。"""
    job = CamJob(GOLDEN_CFG())
    job.load(DXF2)
    pocket = next(f for f in job.result.features if f.kind == RECT_POCKET)
    pocket.params.strategy = PARALLEL_POCKET
    pocket.params.depth = 10.0
    pocket.params.n_passes = 1
    pocket.params.stepover_mm = 2.0          # 绝对行距 2mm
    pocket.params.parallel_axis = "Y"        # 走刀沿 Y，行沿 X 分布
    c = next(c for c in job.result.contours if c.id == pocket.contour_id)
    tool = job.lib.get("T1")
    mv = toolpath.gen_feature_moves(pocket, c, job.result.contours, tool, job.cfg)
    feeds = [m for m in mv if m.kind == FEED]
    assert feeds, "未生成平行清槽刀路"

    # 关闭内壁精修 → 纯扫描：110×160 槽可达 x∈[163.6,268.4]，行距 2mm → 54 行
    mv2 = toolpath.parallel_moves(c.pts, [], tool, [10.0], job.cfg,
                                  axis="Y", stepover=2.0, wall_trim=False)
    feeds2 = [m for m in mv2 if m.kind == FEED]
    xs = sorted({round(m.x, 2) for m in feeds2 if m.x is not None})
    assert len(xs) == 54, f"行数 {len(xs)}"
    ys = [m.y for m in feeds2 if m.y is not None]
    assert abs((max(ys) - min(ys)) - 154.8) < 0.05   # 走刀沿 Y 覆盖槽高

    # 默认 wall_trim=True：每层扫描后绕边界一圈；精修环起点 = 离扫描终点
    # 最近的边界顶点（本例扫描止于右下 → 从右下角切入并闭合，不再回左下）
    mv3 = toolpath.parallel_moves(c.pts, [], tool, [10.0], job.cfg,
                                  axis="Y", stepover=2.0, wall_trim=True)
    n_trim = sum(1 for m in mv3 if m.kind == FEED)
    assert n_trim >= len(feeds2) + 4, "内壁精修环缺失"
    xy = [(m.x, m.y) for m in mv3 if m.x is not None and m.y is not None]
    assert math.dist(xy[-1], (268.4, 369.6)) < 0.5, "精修环应在扫描终点附近切入并闭合"


def test_p12d_parallel_wall_trim():
    """内壁精修：每层扫描后绕边界一圈；凸矩形全程直连仅末尾抬刀。"""
    cfg = GOLDEN_CFG()
    tool = ModelTool(id="T1", diameter=6.0, plunge_f=3000, feed_f=9000)
    ring = [(0, 0), (100, 0), (100, 60), (0, 60)]
    mv = toolpath.parallel_moves(ring, [], tool, [5.0], cfg,
                                 axis="X", stepover=3.6, wall_trim=True)
    retracts = [m for m in mv if m.kind == RETRACT]
    assert len(retracts) == 1, "凸矩形（扫描+内壁环）应全程直连仅末尾抬刀"
    pts = [(m.x, m.y) for m in mv if m.x is not None and m.y is not None]
    # 内壁环闭合：末点回到环起点 = 离扫描终点最近的顶点（右上角附近）
    assert math.dist(pts[-1], (97.4, 57.4)) < 1.0
    # 环沿四壁：末段轨迹的 y 覆盖接近全高（54.8）、x 覆盖接近全宽（94.8）
    tail = pts[-100:]
    ty = [p[1] for p in tail]
    tx = [p[0] for p in tail]
    assert (max(ty) - min(ty)) > 54.0, "内壁环未绕上下壁"
    assert (max(tx) - min(tx)) > 94.0, "内壁环未绕左右壁"
    # 双层：层间同样直连，仍只有末尾一次抬刀
    mv2 = toolpath.parallel_moves(ring, [], tool, [2.5, 5.0], cfg,
                                  axis="X", stepover=3.6, wall_trim=True)
    assert sum(1 for m in mv2 if m.kind == RETRACT) == 1


# P14 偏置清槽：圈间就近衔接 + 从内/从外起刀方向 ------------------------------

def test_p14_pocket_nearest_and_direction():
    """每圈闭合；圈间就近衔接（连刀≈步距而非跨环长斜拉）；inside_out 圈序反转。"""
    cfg = GOLDEN_CFG()
    tool = ModelTool(id="T1", diameter=6.0, plunge_f=3000, feed_f=9000)
    ring = [(0, 0), (100, 0), (100, 80), (0, 80)]

    # 默认 outside_in：首圈=最外环，下刀点远离中心
    mv_out = toolpath.pocket_moves(ring, [], tool, [5.0], cfg, stepover=5.0)
    p_out = (mv_out[0].x, mv_out[0].y)
    # inside_out：首圈=最内环，下刀点靠近中心
    mv_in = toolpath.pocket_moves(ring, [], tool, [5.0], cfg, stepover=5.0,
                                  direction="inside_out")
    p_in = (mv_in[0].x, mv_in[0].y)
    center = (50.0, 40.0)
    assert math.dist(p_out, center) > math.dist(p_in, center) + 20, \
        "起刀方向未反转圈序"

    # 每圈闭合：首圈走刀回到下刀点
    feeds = [(m.x, m.y) for m in mv_out if m.kind == FEED]
    j = 0
    while j < len(feeds) and math.dist(feeds[j], p_out) > 0.01:
        j += 1
    assert j < len(feeds), "首圈未闭合回到起点"
    # 圈间就近：首圈终点 → 下一圈起点的连刀 ≈ 步距（<2×步距），无长斜拉
    assert j + 1 < len(feeds), "缺少第二圈"
    jump = math.dist(feeds[j], feeds[j + 1])
    assert jump < 10.0, f"圈间连刀过长: {jump:.1f}"


# P15 避让岛图层：口袋自动避让 --------------------------------------------------

def test_p15_island_layer():
    """「避让岛」图层闭合环：不生成特征；偏置/平行清槽避让袋内岛（可多个）。"""
    from shapely.geometry import Polygon, Point

    cfg = GOLDEN_CFG()
    tool = ModelTool(id="T1", diameter=6.0, plunge_f=3000, feed_f=9000)

    def rect(x0, y0, w, h):
        return [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h), (x0, y0)]

    ents = [
        Entity(id="E0000", etype="LWPOLYLINE", layer="0",
               points=rect(-50, -50, 500, 400)),           # 外框
        Entity(id="E0001", etype="LWPOLYLINE", layer="加工",
               points=rect(0, 0, 300, 200)),               # 大口袋
        Entity(id="E0002", etype="LWPOLYLINE", layer="避让岛",
               points=rect(40, 40, 40, 30)),               # 袋内岛1
        Entity(id="E0003", etype="LWPOLYLINE", layer="避让岛",
               points=rect(180, 120, 50, 40)),             # 袋内岛2
        Entity(id="E0004", etype="LWPOLYLINE", layer="避让岛",
               points=rect(400, 100, 30, 30)),             # 袋外岛（应跳过）
        Entity(id="E0005", etype="CIRCLE", layer="φ11",
               points=[(60.0, 55.0)], circle=(60.0, 55.0, 11.0)),  # 岛上孔
    ]
    data = DxfData(path="t.dxf", entities=ents, layer_roles={
        "0": ROLE_ZERO, "加工": ROLE_GEOM, "避让岛": ROLE_ISLAND, "φ11": ROLE_DRILL})
    res = recognize(data, cfg, Rules())

    # 1) 避让岛不生成独立特征；岛上孔照常识别
    feat_cids = {f.contour_id for f in res.features}
    in_isls = [c for c in res.contours if c.is_island and c.centroid[0] < 350]
    out_isls = [c for c in res.contours if c.is_island and c.centroid[0] >= 350]
    assert len(in_isls) == 2 and len(out_isls) == 1, "避让岛打标数量错误"
    for c in in_isls + out_isls:
        assert c.id not in feat_cids, "避让岛不应生成加工特征"
    assert any(f.kind == THROUGH_HOLE for f in res.features), "岛上孔未被识别"
    pocket = next(c for c in res.contours
                  if c.layer == "加工" and not c.is_stock_outline)
    assert pocket.id in feat_cids, "口袋特征缺失"

    # 2) 岛收集：child 岛 ∪ 图层岛去重；袋外岛排除
    got = toolpath._collect_islands(pocket, res.contours, cfg)

    def _sig(pts):
        return tuple(sorted((round(x, 3), round(y, 3)) for x, y in pts))

    gotset = {_sig(pts) for pts in got}
    for c in in_isls:
        assert _sig(c.pts) in gotset, "袋内避让岛未参与避让"
    for c in out_isls:
        assert _sig(c.pts) not in gotset, "袋外岛不应参与避让"

    # 3) 偏置清槽 / 平行清槽：走刀点不进入袋内岛（含刀径安全距离）
    feat = next(f for f in res.features if f.contour_id == pocket.id)
    feat.params.depth = 5.0
    feat.params.n_passes = 1
    feat.params.stepover_mm = 5.0
    reach = tool.diameter / 2.0 - cfg.pocket_overcut
    guards = [Polygon(c.pts).buffer(reach * 0.6) for c in in_isls]
    for strat in (OFFSET_POCKET, PARALLEL_POCKET):
        feat.params.strategy = strat
        feat.params.parallel_axis = "X"
        moves = toolpath.gen_feature_moves(feat, pocket, res.contours, tool, cfg)
        feeds = [(m.x, m.y) for m in moves if m.kind == FEED]
        assert feeds, f"{strat} 刀路为空"
        for g in guards:
            for p in feeds:
                assert not g.contains(Point(*p)), f"{strat} 刀路切入避让岛"


# P16 避让岛连刀安全（防回归：环间/段间 FEED 不得斜穿岛） ------------------------

def test_p16_island_link_safety():
    """多避让岛袋的连刀安全：偏置 inside_out（穿岛根因场景）/outside_in、
    平行 X/Y 清槽——任意 FEED 连刀不得越出刀心可达区（越界 >0.1mm 失败）；
    有岛平行清槽抬刀数应远小于跨岛行数（就近接龙：先一侧再换区）。"""
    from shapely.geometry import Polygon, LineString

    cfg = GOLDEN_CFG()
    tool = ModelTool(id="T1", diameter=6.0, plunge_f=3000, feed_f=9000)

    def rect(x0, y0, w, h):
        return [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h), (x0, y0)]

    ring = rect(0, 0, 120, 80)
    isl_a = rect(15, 15, 25, 20)      # 左下岛
    isl_b = rect(70, 40, 30, 25)      # 右上岛
    reach = tool.diameter / 2.0 - cfg.pocket_overcut
    isles = Polygon(isl_a).buffer(0).union(Polygon(isl_b).buffer(0))
    tol = Polygon(ring).difference(isles).buffer(-reach).buffer(0.05)

    def no_escape(moves, msg):
        prev = None
        for m in moves:
            if m.kind == FEED and prev is not None and m.x is not None:
                seg = LineString([prev, (m.x, m.y)])
                assert seg.length <= 1e-9 or \
                    seg.difference(tol).length <= 0.1, \
                    f"{msg}: 连刀穿岛/出袋 {prev} -> {(m.x, m.y)}"
            if m.x is not None and m.y is not None:
                prev = (m.x, m.y)

    for kw in ({"direction": "inside_out"}, {}):
        mv = toolpath.pocket_moves(ring, [isl_a, isl_b], tool, [5.0], cfg,
                                   stepover=3.6, **kw)
        assert mv, "偏置刀路为空"
        no_escape(mv, f"offset {kw.get('direction', 'outside_in')}")

    for axis in ("X", "Y"):
        mv = toolpath.parallel_moves(ring, [isl_a, isl_b], tool, [5.0], cfg,
                                     axis=axis, stepover=3.6, wall_trim=True)
        assert mv, "平行刀路为空"
        no_escape(mv, f"parallel {axis}")
        n_ret = sum(1 for m in mv if m.kind == RETRACT)
        assert n_ret <= 4, \
            f"parallel {axis}: 有岛抬刀 {n_ret} 次，应远小于跨岛行数(约16)"


# P13 手动排序（manual 锁定） ---------------------------------------------------

def test_p13_manual_sort():
    fs = []
    for fid, order in (("F1", 2), ("F2", 1), ("F3", 3)):
        f = Feature(id=fid, kind=UNKNOWN, params=FeatureParams())
        f.order = order
        fs.append(f)
    out = sort_features(fs, [], "manual")
    assert [f.id for f in out] == ["F2", "F1", "F3"]
    assert [f.order for f in out] == [1, 2, 3]     # 自动压缩连续

    # 手动调整 = 移动到第 N 位，其余顺延（模拟 App.reorder_feature 逻辑）
    lst = list(out)
    f = lst.pop(0)          # F2
    lst.insert(2, f)        # F2 → 第 3 位
    for i, ff in enumerate(lst):
        ff.order = i + 1
    out2 = sort_features(lst, [], "manual")
    assert [f.id for f in out2] == ["F1", "F3", "F2"]
    assert [f.order for f in out2] == [1, 2, 3]
