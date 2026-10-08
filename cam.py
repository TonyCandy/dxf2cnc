# -*- coding: utf-8 -*-
"""CAM 管线：DXF → 识别 → 排序 → 刀路 → 按刀分组导出（单刀单文件）。

GUI 与测试共用此模块，保证两条路径行为一致。
"""
from __future__ import annotations

from pathlib import Path

from model import (JobConfig, Tool, ToolLibrary, Move, Feature, Contour,
                   RAPID, FEED, PLUNGE, RETRACT)
import io_dxf
import geom
import recognize
import sort as sortmod
import toolpath
import post_baoyuan
from io_dxf import DxfData
from recognize import Rules, RecognitionResult


class CamJob:
    def __init__(self, cfg: JobConfig | None = None,
                 rules: Rules | None = None,
                 lib: ToolLibrary | None = None):
        self.cfg = cfg or JobConfig()
        self.rules = rules or Rules()
        self.lib = lib or ToolLibrary()
        self.data: DxfData | None = None
        self.result: RecognitionResult | None = None
        self.ordered: list[Feature] = []

    # ------------------------------------------------------------------ load
    def load(self, dxf_path: str | Path) -> RecognitionResult:
        self.data = io_dxf.read_dxf(dxf_path, self.cfg)
        self.result = recognize.recognize(self.data, self.cfg, self.rules)
        self.ordered = []
        return self.result

    # ------------------------------------------------------------------ sort
    def sort(self, strategy: str = "around") -> list[Feature]:
        assert self.result is not None
        self.ordered = sortmod.sort_features(
            self.result.features, self.result.contours, strategy)
        return self.ordered

    # --------------------------------------------------------------- toolpath
    def moves_by_tool(self) -> dict[str, list[Move]]:
        """已排序特征 → {tool_id: Move[]}。跳过 enabled=False。"""
        assert self.result is not None
        if not self.ordered:
            self.sort()
        by_id = {c.id: c for c in self.result.contours}
        out: dict[str, list[Move]] = {}
        for f in self.ordered:
            if not f.params.enabled:
                continue
            c = by_id.get(f.contour_id)
            if c is None or f.params.depth <= 0:
                continue
            try:
                tool = self.lib.get(f.params.tool_id)
            except KeyError:
                f.warnings.append(f"未知刀具 {f.params.tool_id}，跳过")
                continue
            mv = toolpath.gen_feature_moves(f, c, self.result.contours, tool, self.cfg)
            if mv:
                out.setdefault(f.params.tool_id, []).extend(mv)
        return out

    def moves_by_feature(self) -> dict[str, list[Move]]:
        """已排序特征 → {feature_id: Move[]}（全部启用特征，不分刀）。

        过滤规则与 moves_by_tool 一致（enabled / 有轮廓 / depth>0 / 已知刀具），
        供覆盖区按特征高亮、baoyuan 快照按特征存刀路使用。
        """
        assert self.result is not None
        if not self.ordered:
            self.sort()
        by_id = {c.id: c for c in self.result.contours}
        out: dict[str, list[Move]] = {}
        for f in self.ordered:
            if not f.params.enabled:
                continue
            c = by_id.get(f.contour_id)
            if c is None or f.params.depth <= 0:
                continue
            try:
                tool = self.lib.get(f.params.tool_id)
            except KeyError:
                continue          # 警告已在 moves_by_tool 记过，不重复
            mv = toolpath.gen_feature_moves(f, c, self.result.contours, tool, self.cfg)
            if mv:
                out[f.id] = mv
        return out

    # ---------------------------------------------------------------- export
    def export_by_tool(self, out_dir: str | Path, basename: str) -> dict[str, str]:
        """每把刀一个 .cnc 文件：{basename}-{tool}.cnc。返回 {tool_id: 路径}。"""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: dict[str, str] = {}
        for tool_id, moves in self.moves_by_tool().items():
            tool = self.lib.get(tool_id)
            path = out_dir / f"{basename}-{tool_id}.cnc"
            post_baoyuan.write_cnc(str(path), moves, tool, self.cfg)
            written[tool_id] = str(path)
        return written
