# -*- coding: utf-8 -*-
"""CNC 几何拓扑对比工具：提取每段加工闭环（形心/Z深/半径/段数）做差集对比。

用法（也供测试引用）：
    from cnc_compare import parse_loops, compare
"""
from __future__ import annotations

import math
import re


def parse_cnc(path: str) -> list[dict]:
    """解析 .cnc → 闭环列表。每环：{cx, cy, z, r_avg, n, kind}。

    环 = 一次 PLUNGE 之后连续的 XY 走刀序列（到 Z 变化/抬刀为止）。
    直下刀（无 XY 走刀）记为点环 {kind:'point'}。
    """
    with open(path, newline="", encoding="ascii", errors="replace") as fh:
        text = fh.read()
    lines = text.replace("\r\n", "\n").split("\n")

    loops: list[dict] = []
    pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    cur_z = 0.0
    cur_pts: list[tuple[float, float]] = []
    loop_open = False

    num = r"([XYZ])(-?\d+\.?\d*)"

    def flush():
        nonlocal cur_pts, loop_open
        if loop_open and cur_pts:
            if len(cur_pts) >= 3:
                xs = [p[0] for p in cur_pts]
                ys = [p[1] for p in cur_pts]
                cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
                rs = [math.dist(p, (cx, cy)) for p in cur_pts]
                r_avg = sum(rs) / len(rs)
                r_dev = max(rs) - min(rs)
                loops.append({"cx": round(cx, 2), "cy": round(cy, 2),
                              "z": round(cur_z, 3), "r": round(r_avg, 4),
                              "r_dev": round(r_dev, 4), "n": len(cur_pts),
                              "kind": "loop"})
            else:
                loops.append({"cx": round(cur_pts[0][0], 2),
                              "cy": round(cur_pts[0][1], 2),
                              "z": round(cur_z, 3), "n": len(cur_pts),
                              "kind": "point"})
        cur_pts = []
        loop_open = False

    for line in lines:
        if not line.startswith("N"):
            continue
        body = re.sub(r"^N\d+", "", line)
        axes = dict((a, float(v)) for a, v in re.findall(num, body))
        if "G0" in body:
            flush()
            pos.update(axes)
            continue
        is_g1 = "G1" in body
        if not axes and not is_g1:
            continue
        if "Z" in axes and not {"X", "Y"} & set(axes):
            # 纯 Z 变化：下刀（G1）或已由 G0 flush
            if is_g1:
                flush()
                cur_z = axes["Z"]
                pos.update(axes)
                loop_open = True  # 开始一段（可能无 XY → 点环）
                cur_pts = []
            continue
        if {"X", "Y"} & set(axes):
            if not loop_open:
                loop_open = True
                cur_pts = []
            # 保留上一点便于起点闭合
            cur_pts.append((axes.get("X", pos["X"]), axes.get("Y", pos["Y"])))
            pos.update(axes)
    flush()
    return loops


def compare(gold: list[dict], gen: list[dict],
            tol_c: float = 0.5, tol_z: float = 0.01, tol_r: float = 0.05
            ) -> tuple[int, int, list[str]]:
    """对比两组环。返回 (匹配数, 黄金环数, 差异说明列表)。"""
    diffs: list[str] = []
    used = [False] * len(gen)
    matched = 0
    for g in gold:
        best_i, best_score = -1, 1e9
        for i, m in enumerate(gen):
            if used[i]:
                continue
            dc = math.dist((g["cx"], g["cy"]), (m["cx"], m["cy"]))
            dz = abs(g["z"] - m["z"])
            dr = abs(g.get("r", 0) - m.get("r", 0))
            if dc < tol_c and dz < tol_z and dr < tol_r:
                score = dc + dz + dr
                if score < best_score:
                    best_i, best_score = i, score
        if best_i >= 0:
            used[best_i] = True
            matched += 1
        else:
            diffs.append(f"黄金环未匹配: {g}")
    for i, m in enumerate(gen):
        if not used[i]:
            diffs.append(f"多出生成环: {m}")
    return matched, len(gold), diffs
