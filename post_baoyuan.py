# -*- coding: utf-8 -*-
"""L5：后处理。中性 Move[] → 宝元（LNC）.cnc 文本。

格式规则（从实机黄金样例逐字反推，见《项目需求总结更新》§9）：
- 文件头："%"+CRLF、一个空格行、N3G90 … N12G43H{t}，回零点 home 可配
- 行号 N3 起每行 +1；X/Y/Z 4 位小数、F 1 位、S 整数；字地址间无空格
- G0：输出相对模态位置变化的轴（首个定位写全 XYZ）
- G1：F 变化时带 "G1..F.."，F 相同且上一行同为走刀时只写轴值
- 屏蔽 G02/G03（全部折线）；换刀不做中途 —— 单刀单文件，上机手动换
- 尾部：G0X{home} → M5 → G53Z0 → M30 → "%"
"""
from __future__ import annotations

import math

from model import (JobConfig, Move, Tool, ToolLibrary,
                   RAPID, FEED, PLUNGE, RETRACT)

CRLF = "\r\n"


def _fmt_axis(prefix: str, v: float | None) -> str:
    return f"{prefix}{v:.4f}"


def _changed(cur: dict[str, float | None], last: dict[str, float | None],
             axes: str) -> list[str]:
    out = []
    for a in axes:
        v = cur.get(a)
        if v is not None and (last.get(a) is None or abs(v - last[a]) > 1e-9):
            out.append(_fmt_axis(a, v))
    return out


def emit_cnc(moves: list[Move], tool: Tool, cfg: JobConfig) -> str:
    """Move[] → 完整 .cnc 文本（单刀程序）。"""
    n = cfg.line_num_start - 1  # 先减，下面每行先 +1

    def N() -> int:
        nonlocal n
        n += 1
        return n

    lines: list[str] = []

    # ---- 头 ----
    lines.append("%")
    lines.append(" ")
    lines.append(f"N{N()}G90")
    lines.append(f"N{N()}G54")
    lines.append(f"N{N()}G53Z0")
    lines.append(f"N{N()}G0X{cfg.home[0]:.4f}Y{cfg.home[1]:.4f}")
    lines.append(f"N{N()}M05")
    lines.append(f"N{N()}M6T{tool.number}")
    lines.append(f"N{N()}G90")
    lines.append(f"N{N()}G54")
    lines.append(f"N{N()}M3S{int(tool.spindle)}")
    lines.append(f"N{N()}G43H{tool.number}")

    # ---- 运动 ----
    pos: dict[str, float | None] = {"X": cfg.home[0], "Y": cfg.home[1], "Z": 0.0}
    last_f: float | None = None
    last_motion_g1 = False      # 上一运动行是否 G1（决定能否省 G1）
    first_move = True

    for m in moves:
        if m.kind not in (RAPID, RETRACT, PLUNGE, FEED):
            continue  # COMMENT 等不输出
        cur = {"X": m.x, "Y": m.y, "Z": m.z}

        if m.kind in (RAPID, RETRACT):
            axes = _changed(cur, pos, "XYZ")
            if not axes:
                continue
            if first_move:
                # 首个定位：全三轴（与黄金样例一致）
                axes = [_fmt_axis(a, cur[a]) for a in "XYZ" if cur[a] is not None]
            lines.append(f"N{N()}G0" + "".join(axes))
            for a in "XYZ":
                if cur[a] is not None:
                    pos[a] = cur[a]
            first_move = False
            last_motion_g1 = False
            continue

        # G1 类（PLUNGE / FEED）
        axes = _changed(cur, pos, "XYZ")
        if not axes:
            continue
        f = m.f if m.f is not None else (last_f or 3000.0)
        parts: list[str] = []
        if last_motion_g1 and f == last_f:
            parts = list(axes)  # 纯轴值（对齐黄金：N16X..Y..）
        else:
            parts = ["G1"] + axes
            if f != last_f:
                parts.append(f"F{f:.1f}")
        lines.append(f"N{N()}" + "".join(parts))
        for a in "XYZ":
            if cur[a] is not None:
                pos[a] = cur[a]
        last_f = f
        last_motion_g1 = True

    # ---- 尾 ----
    lines.append(f"N{N()}G0X{cfg.home[0]:.4f}Y{cfg.home[1]:.4f}")
    lines.append(f"N{N()}M5")
    lines.append(f"N{N()}G53Z0")
    lines.append(f"N{N()}M30")
    lines.append("%")

    return CRLF.join(lines) + CRLF


def write_cnc(path: str, moves: list[Move], tool: Tool, cfg: JobConfig) -> None:
    """写出 .cnc（ASCII + CRLF）。"""
    text = emit_cnc(moves, tool, cfg)
    with open(path, "w", encoding="ascii", newline="") as fh:
        fh.write(text)
