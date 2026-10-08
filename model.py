# -*- coding: utf-8 -*-
"""数据模型：L1 实体 / L2 轮廓 / L3 特征 / 刀具 / 全局配置 / L4 中性刀路。

对应《项目需求总结更新》第 5 章。内部模型与 CNC 文本无关；
只有导出时才由 post_baoyuan 消费 L4 Move。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

# ----------------------------------------------------------------------------
# L1 原始实体（io_dxf 产出）
# ----------------------------------------------------------------------------

@dataclass
class Entity:
    id: str
    etype: str                       # LINE / CIRCLE / ARC / LWPOLYLINE / POLYLINE
    layer: str
    points: list[tuple[float, float]] = field(default_factory=list)  # 已离散折线
    circle: tuple[float, float, float] | None = None  # CIRCLE: (cx, cy, d)
    source_handle: str = ""

    @property
    def is_circle(self) -> bool:
        return self.circle is not None


# ----------------------------------------------------------------------------
# L2 闭环轮廓（geom 产出）
# ----------------------------------------------------------------------------

@dataclass
class Contour:
    id: str
    entity_ids: list[str] = field(default_factory=list)
    closed: bool = False
    pts: list[tuple[float, float]] = field(default_factory=list)  # 闭合环 pts[0]==pts[-1]
    area: float = 0.0
    perimeter: float = 0.0
    centroid: tuple[float, float] = (0.0, 0.0)
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # minx,miny,maxx,maxy
    is_circle: bool = False
    diameter: float | None = None
    is_rect: bool = False
    size: tuple[float, float] | None = None       # 宽, 高（矩形）
    corner_r: float | None = None                  # 圆角半径（圆角矩形）
    parent_id: str | None = None                   # 所属外环（包含关系）
    child_ids: list[str] = field(default_factory=list)
    is_stock_outline: bool = False                 # 最大外框，默认不加工
    is_island: bool = False                        # 避让岛图层环（口袋避让，不加工）
    layer: str = ""                                # 主来源图层

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


# ----------------------------------------------------------------------------
# L3 加工特征（GUI 编辑的核心）
# ----------------------------------------------------------------------------

# 特征类型
THROUGH_HOLE = "THROUGH_HOLE"        # 通孔（螺旋切透）
COUNTERBORE = "COUNTERBORE"          # 沉孔（同心大圆，浅）
DRILL = "DRILL"                      # 钻孔（小孔，可直下刀或螺旋）
CIRCULAR_POCKET = "CIRCULAR_POCKET"  # 圆形凹槽
RECT_POCKET = "RECT_POCKET"          # 矩形/圆角矩形凹槽
POCKET = "POCKET"                    # 一般凹槽
ON_VECTOR = "ON_VECTOR"              # 沿中心线（开放链）
OUTLINE = "OUTLINE"                  # 外轮廓切割（默认 STOCK 跳过）
UNKNOWN = "UNKNOWN"

# 策略
SPIRAL = "spiral"                    # 螺旋下刀（每层一整圈折线圆）
DIRECT_PLUNGE = "direct"             # 直下刀（沉头刀在已开孔处）
OFFSET_POCKET = "offset_pocket"      # 区域清除-偏置
PARALLEL_POCKET = "parallel_pocket"  # 区域清除-平行（刀轨平行 X/Y，zigzag 往复）
CONTOUR = "contour"                  # 轮廓（内/外）
ON_VECTOR_S = "on_vector"            # 沿中心线
VECTOR_CUT = "vector_cut"            # 沿矢量加工（开放链专属：刀在矢量左/右/居中）


@dataclass
class FeatureParams:
    tool_id: str = "T1"
    depth: float = 0.0               # 正数，切到 Z=-depth
    stepdown: float = 0.0            # 每刀切深；0 表示用 n_passes 等分
    n_passes: int | None = None      # 与 stepdown 二选一
    strategy: str = SPIRAL
    climb: bool = True               # 顺铣
    stepover_factor: float = 0.9     # 行距系数（× 刀径），偏置/平行清槽共用
    stepover_mm: float | None = None # 绝对行距 mm；非 None 时优先于系数
    parallel_axis: str = "X"         # 平行清槽走刀方向：X / Y
    pocket_direction: str = "outside_in"  # 偏置清槽起刀：outside_in 外→内/ inside_out 内→外
    vector_side: str = "center"      # 沿矢量加工刀位：left / right / center
    allowance: float = 0.0           # 余量：正=少切，负=过切
    approach_z: float | None = None  # 直下刀模式的 G0 接近高度（如 Z1）
    enabled: bool = True
    feed_override: float | None = None
    plunge_override: float | None = None

    def pass_depths(self) -> list[float]:
        """按 stepdown 或 n_passes 计算每层 Z 深（正数递增到 depth）。"""
        if self.depth <= 0:
            return []
        if self.n_passes:
            n = self.n_passes
            return [round(self.depth * (i + 1) / n, 4) for i in range(n)]
        if self.stepdown > 0:
            ds: list[float] = []
            z = 0.0
            while z < self.depth - 1e-9:
                z = min(z + self.stepdown, self.depth)
                ds.append(round(z, 4))
            return ds
        return [self.depth]


@dataclass
class Feature:
    id: str
    kind: str = UNKNOWN
    contour_id: str = ""
    label: str = ""
    group_key: str = ""              # 分组视图："φ11 通孔" / "110×160 沉槽"
    location_id: str = ""            # 同一站（同心组）
    params: FeatureParams = field(default_factory=FeatureParams)
    paired_ids: list[str] = field(default_factory=list)  # 同心伙伴
    order: int = 0                   # 排序后序号
    warnings: list[str] = field(default_factory=list)

    def kind_cn(self) -> str:
        return {
            THROUGH_HOLE: "通孔", COUNTERBORE: "沉孔", DRILL: "钻孔",
            CIRCULAR_POCKET: "圆槽", RECT_POCKET: "矩形沉槽",
            POCKET: "凹槽", ON_VECTOR: "沿矢量", OUTLINE: "外轮廓",
            UNKNOWN: "未识别",
        }.get(self.kind, self.kind)


# ----------------------------------------------------------------------------
# 刀具库
# ----------------------------------------------------------------------------

@dataclass
class Tool:
    id: str = "T1"
    number: int = 1
    diameter: float = 6.0
    type: str = "endmill"            # endmill / drill / countersink
    usage: str = ""
    spindle: float = 18000.0
    plunge_f: float = 3000.0
    feed_f: float = 9000.0
    max_stepdown: float = 10.0
    stepover_factor: float = 0.9

    @staticmethod
    def load_library(path: str | Path) -> list["Tool"]:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return [Tool(**t) for t in data["tools"]]

    @staticmethod
    def default_library() -> list["Tool"]:
        return [
            Tool(id="T1", number=1, diameter=6.0, type="endmill",
                 usage="开槽/轮廓/φ11 螺旋", spindle=18000, plunge_f=3000, feed_f=9000),
            Tool(id="T2", number=2, diameter=4.0, type="endmill",
                 usage="小孔钻孔 φ5.5/沉头孔", spindle=18000, plunge_f=3000, feed_f=9000),
        ]


class ToolLibrary:
    def __init__(self, tools: list[Tool] | None = None):
        self.tools = {t.id: t for t in (tools or Tool.default_library())}

    def get(self, tool_id: str) -> Tool:
        if tool_id not in self.tools:
            raise KeyError(f"刀具库中不存在 {tool_id}")
        return self.tools[tool_id]


# ----------------------------------------------------------------------------
# 全局加工配置
# ----------------------------------------------------------------------------

@dataclass
class JobConfig:
    stock_thickness: float = 20.0    # 板厚（"切透"按钮用）
    safe_z: float = 30.0
    home: tuple[float, float] = (0.0, 3000.0)   # 回零点（用户确认默认 Y3000）
    hole_overcut: float = 0.0        # 孔螺旋径向过切（默认 0：过切用“刀径改小”法实现）
    pocket_overcut: float = 0.0      # 口袋单边过切（默认 0：过切用“刀径改小”法实现）
    chord_error: float = 0.005       # 圆离散弦高 mm（对齐黄金：φ11→52段、φ23→96段）
    join_tol: float = 0.05           # 拼环端点容差
    line_num_start: int = 3          # 实机从 N3 起
    # 特征识别阈值
    eps_concentric: float = 0.5
    roundness: float = 0.98
    rect_angle_tol: float = 2.0
    diameter_snap: float = 0.15
    standard_holes: list[float] = field(default_factory=lambda: [11.0, 23.0, 5.5])
    ignore_layers: list[str] = field(default_factory=lambda: [
        "尺寸线层", "中心线层", "虚线层", "细实线层"])
    machine_travel: tuple[float, float] = (2000.0, 3000.0)  # XY 行程粗检


# ----------------------------------------------------------------------------
# L4 中性刀路（后处理输入）
# ----------------------------------------------------------------------------

RAPID = "RAPID"          # G0
FEED = "FEED"            # G1 走刀
PLUNGE = "PLUNGE"        # G1 下刀
RETRACT = "RETRACT"      # G0 抬刀
COMMENT = "COMMENT"


@dataclass
class Move:
    kind: str             # RAPID / FEED / PLUNGE / RETRACT / COMMENT
    x: float | None = None
    y: float | None = None
    z: float | None = None
    f: float | None = None
    tool: str | None = None
    spindle: float | None = None
    text: str = ""        # COMMENT 用


@dataclass
class ToolpathProgram:
    """一把刀对应一个程序（单刀单文件，上机手动换刀换文件）。"""
    tool_id: str
    moves: list[Move] = field(default_factory=list)
    spindle: float = 18000.0
