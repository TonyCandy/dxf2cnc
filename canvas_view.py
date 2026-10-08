# -*- coding: utf-8 -*-
"""画布视图：DXF 几何 + 特征高亮 + 顺序号 + 刀路预览。

交互：滚轮缩放（以鼠标为中心）、中键拖平移、左键点选特征。
坐标系：DXF Y 向上 / 屏幕 Y 向下，视图自动适应。
"""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk

from model import (Contour, Feature, Move, RAPID, FEED, PLUNGE, RETRACT)

# 配色
C_BG = "#101418"
C_GEOM = "#5a6a7a"          # 原始几何
C_IGNORED = "#7a8494"       # 参考层（提亮以便看清尺寸/文字）
C_STOCK = "#8899aa"         # 外框（虚线）
C_ISLAND = "#e08a3c"        # 避让岛（橙虚线，口袋清料时保留）
C_HIGHLIGHT = "#ff5533"     # 选中
C_FEATURE = "#2ea8e6"       # 特征轮廓
C_ORDER = "#ffd24a"         # 顺序号（兼：覆盖区选中高亮亮黄）
C_RAPID = "#7a5ae6"         # G0
C_FEED = "#22cc88"          # 走刀
C_AXIS_X = "#2a7f3f"        # X 轴（绿）
C_AXIS_Y = "#c23a3a"        # Y 轴（红）
C_COVERAGE = "#1f7a4d"      # 覆盖区（深绿，统一不按刀分色）
C_COVER_HOLE = "#ff5533"    # 覆盖区漏切（洞）高亮橙红，实心不半透明


class CanvasView(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.canvas = tk.Canvas(self, bg=C_BG, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # 视图变换：world → screen = (wx*scale + ox, -wy*scale + oy)
        self.scale = 1.0
        self.ox = 0.0
        self.oy = 0.0
        self._fit_done = False

        # 数据
        self.contours: list[Contour] = []
        # 忽略层显示对象：{"kind": "polyline"/"circle"/"text"/"point", ...}
        self.ignored_objs: list[dict] = []
        self.features: list[Feature] = []
        self.moves: list[Move] = []          # 刀路预览
        self.show_geom = tk.BooleanVar(value=True)
        self.show_ignored = tk.BooleanVar(value=False)
        self.show_order = tk.BooleanVar(value=True)
        self.show_toolpath = tk.BooleanVar(value=False)
        self.show_coverage = tk.BooleanVar(value=False)   # 覆盖区（默认不勾选）
        # 覆盖区数据（shapely Polygon/MultiPolygon）：整体并集 + 每特征分区
        self.coverage = None                  # 全部刀 FEED 覆盖并集
        self.feature_coverage: dict = {}      # fid → 该特征覆盖区
        self._cov_items: dict[str, int] = {}  # fid → 特征覆盖区 polygon item
        self._coverage_item: int | None = None  # 整体覆盖区 item（不参与特征换色）

        # item → feature_id 映射（高亮/点选）：fid → [(item_id, 原色), ...]
        self._feat_items: dict[str, list[tuple[int, str]]] = {}
        self._selected: set[str] = set()

        self._bind_events()

    # ------------------------------------------------------------ 变换
    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        return (x * self.scale + self.ox, -y * self.scale + self.oy)

    def to_world(self, sx: float, sy: float) -> tuple[float, float]:
        return ((sx - self.ox) / self.scale, -(sy - self.oy) / self.scale)

    # ------------------------------------------------------------ 数据设置
    def set_data(self, contours: list[Contour], features: list[Feature],
                 ignored_pts: list[list[tuple[float, float]]]
                 | list[dict] | None = None):
        """设置板件数据。ignored_pts 兼容两种入参：
        旧格式（折线点列表的列表）→ 内部转为 polyline 显示对象；
        新格式（io_dxf/cnc_gui 的显示对象 dict 列表，含折线/圆/文字）→ 直接使用。"""
        self.contours = contours
        self.features = features
        objs: list[dict] = []
        for it in ignored_pts or []:
            if isinstance(it, dict):
                objs.append(it)
            elif len(it) >= 2:
                objs.append({"kind": "polyline", "pts": it})
        self.ignored_objs = objs
        self.moves = []
        self.coverage = None            # 切板清覆盖区（由调用方随后重新设置）
        self.feature_coverage = {}
        self._cov_items = {}
        self._coverage_item = None
        self._selected.clear()
        self._fit_done = False
        self.redraw()

    def set_toolpath(self, moves: list[Move] | None):
        self.moves = moves or []
        self.show_toolpath.set(bool(self.moves))
        self.redraw()

    def set_coverage(self, polygon):
        """整体覆盖区（全部刀 FEED 并集），None 清除。"""
        self.coverage = polygon
        self.redraw()

    def set_feature_coverage(self, cov_map: dict):
        """特征覆盖区 {fid: Polygon}，供点选高亮联动。"""
        self.feature_coverage = cov_map or {}
        self.redraw()

    def set_data_keep(self, features: list[Feature]):
        """保留几何，仅更新特征列表（参数改动后刷新）。"""
        self.features = features
        self.moves = []
        self.redraw()

    # ------------------------------------------------------------ 绘制
    def redraw(self):
        c = self.canvas
        c.delete("all")
        self._feat_items.clear()
        self._cov_items.clear()
        if not self.contours:
            return
        if not self._fit_done:
            self._fit()
            self._fit_done = True

        self._draw_axes()
        by_id = {f.id: f for f in self.features}

        # 忽略层：按对象类型绘制（折线/圆/文字/点）
        if self.show_ignored.get():
            for o in self.ignored_objs:
                self._draw_ignored_obj(o)

        # 原始几何（细）；避让岛以橙色虚线区分显示
        if self.show_geom.get():
            for ct in self.contours:
                if ct.is_stock_outline:
                    continue
                if ct.is_island:
                    self._draw_contour(ct, C_ISLAND, 1, dash=(3, 3))
                    continue
                color = C_FEATURE if ct.id in {f.contour_id for f in self.features} else C_GEOM
                w = 1
                self._draw_contour(ct, color, w, dash=None)

        # 外框虚线
        for ct in self.contours:
            if ct.is_stock_outline:
                self._draw_contour(ct, C_STOCK, 1, dash=(4, 4))

        # 合并标注（序号+直径一行，如 "1·φ11"）：
        # 同心圆组按半径分层 —— 小圆放内部，大的放前两圆之间的环带；
        # 环带像素宽 < 文字宽时放圆外右侧，同组多个向上错开，不加引线。
        by_contour = {ct.id: ct for ct in self.contours}
        circles: dict[tuple[float, float], list[tuple[Feature, Contour]]] = {}
        others: dict[tuple[float, float], list[tuple[Feature, Contour]]] = {}
        for f in self.features:
            if not f.params.enabled:
                continue
            ct = by_contour.get(f.contour_id)
            if ct is None:
                continue
            key = (round(ct.centroid[0], 1), round(ct.centroid[1], 1))
            target = circles if (ct.is_circle and ct.diameter) else others
            target.setdefault(key, []).append((f, ct))

        font_small = ("Segoe UI", 8)
        for (cx, cy), items in circles.items():
            items.sort(key=lambda t: (t[1].diameter or 0))
            prev_r = 0.0
            outside = 0
            for f, ct in items:
                r = (ct.diameter or 0) / 2
                dia_txt = f"φ{ct.diameter:g}"
                text = f"{f.order}·{dia_txt}" if (f.order and self.show_order.get()) else dia_txt
                est_px = len(text) * 7 + 4          # 8 号字约 7px/字符
                band_px = (r if prev_r == 0 else r - prev_r) * self.scale
                if band_px >= est_px:
                    # 放圆内 / 环带中部（45° 方向）
                    rm = (prev_r + r) / 2 if prev_r else 0.0
                    wx = cx + rm * 0.7071
                    wy = cy + rm * 0.7071
                    sx, sy = self.to_screen(wx, wy)
                    c.create_text(sx, sy, text=text, fill=C_ORDER, font=font_small)
                else:
                    # 圆外右侧，同组向上错开一行
                    sx, sy = self.to_screen(cx + r, cy)
                    c.create_text(sx + 6, sy - outside * 14, text=text,
                                  fill=C_ORDER, font=font_small, anchor=tk.W)
                    outside += 1
                prev_r = r

        # 非圆特征（沉槽等）：形心放序号；同形心多个（嵌套矩形）向上错开
        for (cx, cy), items in others.items():
            for i, (f, _ct) in enumerate(items):
                if not f.order or not self.show_order.get():
                    continue
                sx, sy = self.to_screen(cx, cy)
                c.create_text(sx, sy - i * 14, text=str(f.order), fill=C_ORDER,
                              font=("Segoe UI", 9, "bold"))

        # 覆盖区（刀心轨迹下层）：整体并集 + 每特征分区（供点选换色高亮）
        if self.show_coverage.get():
            if self.coverage is not None:
                self._draw_coverage(self.coverage, C_COVERAGE)
            for fid, poly in self.feature_coverage.items():
                if poly is not None:
                    self._draw_coverage(poly, C_COVERAGE, fid=fid)

        # 刀路预览
        if self.show_toolpath.get() and self.moves:
            self._draw_toolpath()

        self._apply_selection()

    def _draw_contour(self, ct: Contour, color: str, width: int, dash=None):
        c = self.canvas
        pts = ct.pts
        if len(pts) >= 2:
            # 开放矢量：末端画箭头指示朝向（矢量起点→终点），起点画小圆圈
            open_line = (not ct.closed) and pts[0] != pts[-1]
            scr = [coord for p in pts for coord in self.to_screen(*p)]
            item = c.create_line(scr, fill=color, width=width, dash=dash,
                                 capstyle=tk.ROUND, tags=("geom", ct.id),
                                 arrow=tk.LAST if open_line else None,
                                 arrowshape=(9, 11, 4))
            if open_line:
                sx, sy = self.to_screen(*pts[0])
                d = 3.5
                c.create_oval(sx - d, sy - d, sx + d, sy + d,
                              outline=color, width=1, tags=("geom", ct.id))
            fid = next((f.id for f in self.features if f.contour_id == ct.id), None)
            if fid:
                self._feat_items.setdefault(fid, []).append((item, color))

    def _draw_polyline(self, pts, color, width):
        if len(pts) < 2:
            return
        scr = [coord for p in pts for coord in self.to_screen(*p)]
        self.canvas.create_line(scr, fill=color, width=width)

    def _draw_coverage(self, poly, fill_color, fid: str | None = None):
        """覆盖区：外环半透明深绿 + 每个洞单独橙红实心（漏切区高亮）。

        MultiPolygon 逐个 geoms 画；每个 geoms 先画 exterior，再对每个
        interior 洞单独 create_polygon —— 禁止与外环平铺（Tk 奇偶填充
        对多洞+共享顶点判定失效，洞会被填满，已实测）。
        洞不登记 _cov_items，固定 C_COVER_HOLE，不随点选换色。
        """
        c = self.canvas
        polys = list(getattr(poly, "geoms", [poly]))
        for pg in polys:
            ext = getattr(pg, "exterior", None)
            if ext is None:
                continue
            scr: list[float] = []
            for p in ext.coords:                 # shapely exterior 已闭合
                scr.extend(self.to_screen(*p))
            item = c.create_polygon(scr, fill=fill_color, outline="",
                                    stipple="gray50")
            for hole in pg.interiors:            # 每个洞单独画：漏切橙红实心
                hscr: list[float] = []
                for p in hole.coords:
                    hscr.extend(self.to_screen(*p))
                c.create_polygon(hscr, fill=C_COVER_HOLE, outline="")
            if fid is not None:
                self._cov_items[fid] = item
            else:
                self._coverage_item = item

    def _draw_ignored_obj(self, o: dict):
        """忽略层显示对象：折线 / 圆 / 文字 / 点（Y 翻转由 to_screen 统一处理）。"""
        c = self.canvas
        kind = o.get("kind")
        if kind == "polyline":
            self._draw_polyline(o.get("pts") or [], C_IGNORED, 1)
        elif kind == "circle":
            cx, cy, d = o["circle"]
            sx, sy = self.to_screen(cx, cy)
            r = max(d / 2 * self.scale, 1.0)   # 缩太小也保底 1px 半径
            c.create_oval(sx - r, sy - r, sx + r, sy + r,
                          outline=C_IGNORED, width=1)
        elif kind == "text":
            sx, sy = self.to_screen(*o["pos"])
            c.create_text(sx, sy, text=o.get("text", ""), fill=C_IGNORED,
                          font=("Segoe UI", 8), anchor=tk.W)
        elif kind == "point":
            sx, sy = self.to_screen(*o["pos"])
            c.create_oval(sx - 1.5, sy - 1.5, sx + 1.5, sy + 1.5,
                          fill=C_IGNORED, outline="")

    def _draw_axes(self):
        c = self.canvas
        w = c.winfo_width()
        h = c.winfo_height()
        ox, _ = self.to_screen(0, 0)
        _, oy = self.to_screen(0, 0)
        if 0 <= ox <= w:
            c.create_line(ox, 0, ox, h, fill=C_AXIS_Y, width=1)
        if 0 <= oy <= h:
            c.create_line(0, oy, w, oy, fill=C_AXIS_X, width=1)

    def _draw_toolpath(self):
        c = self.canvas
        cur = None
        prev = None
        for m in self.moves:
            if m.kind == RAPID or m.kind == RETRACT:
                target = (m.x if m.x is not None else (prev[0] if prev else 0),
                          m.y if m.y is not None else (prev[1] if prev else 0)) \
                    if (m.x is not None or m.y is not None) else None
                if target and prev:
                    p0 = self.to_screen(*prev)
                    p1 = self.to_screen(*target)
                    c.create_line(*p0, *p1, fill=C_RAPID, width=1, dash=(2, 3))
                if target:
                    prev = target
            elif m.kind in (FEED, PLUNGE):
                if m.x is not None and m.y is not None:
                    if prev:
                        p0 = self.to_screen(*prev)
                        p1 = self.to_screen(m.x, m.y)
                        color = C_FEED if m.kind == FEED else "#e6b800"
                        c.create_line(*p0, *p1, fill=color, width=1)
                    prev = (m.x, m.y)

    # ------------------------------------------------------------ 视图
    def _fit(self):
        c = self.canvas
        w = max(c.winfo_width(), 100)
        h = max(c.winfo_height(), 100)
        xs, ys = [], []
        for ct in self.contours:
            b = ct.bbox
            xs.extend([b[0], b[2]])
            ys.extend([b[1], b[3]])
        if not xs:
            return
        margin = 40
        sx = (w - 2 * margin) / max(max(xs) - min(xs), 1e-6)
        sy = (h - 2 * margin) / max(max(ys) - min(ys), 1e-6)
        self.scale = min(sx, sy)
        self.ox = w / 2 - (min(xs) + max(xs)) / 2 * self.scale
        self.oy = h / 2 + (min(ys) + max(ys)) / 2 * self.scale

    def fit_view(self):
        self._fit_done = False
        self.redraw()

    # ------------------------------------------------------------ 交互
    def _bind_events(self):
        c = self.canvas
        c.bind("<MouseWheel>", self._on_wheel)          # Windows
        c.bind("<Button-4>", self._on_wheel)            # Linux
        c.bind("<Button-5>", self._on_wheel)
        c.bind("<ButtonPress-2>", self._on_pan_start)
        c.bind("<B2-Motion>", self._on_pan_move)
        c.bind("<ButtonRelease-1>", self._on_click)

    def _on_wheel(self, e):
        factor = 1.15 if (e.delta > 0 or e.num == 4) else 1 / 1.15
        mx, my = e.x, e.y
        wx, wy = self.to_world(mx, my)
        self.scale *= factor
        self.ox = mx - wx * self.scale
        self.oy = my + wy * self.scale
        self.redraw()

    def _on_pan_start(self, e):
        self._pan_start = (e.x, e.y, self.ox, self.oy)

    def _on_pan_move(self, e):
        if not hasattr(self, "_pan_start"):
            return
        x0, y0, ox0, oy0 = self._pan_start
        self.ox = ox0 + (e.x - x0)
        self.oy = oy0 + (e.y - y0)
        self.redraw()

    def _on_click(self, e):
        # 就近特征反查（半径 12px 内）
        wx, wy = self.to_world(e.x, e.y)
        best = None
        best_d = 1e9
        for f in self.features:
            ct = next((x for x in self.contours if x.id == f.contour_id), None)
            if ct is None:
                continue
            # 距形心（简化，小特征足够；大特征用外扩 bbox 距离）
            d = math.dist((wx, wy), ct.centroid)
            b = ct.bbox
            # 点在 bbox 外时算到 bbox 的距离
            dx = max(b[0] - wx, 0, wx - b[2])
            dy = max(b[1] - wy, 0, wy - b[3])
            d = math.hypot(dx, dy) if (dx or dy) else 0
            if d < best_d:
                best_d, best = d, f.id
        if best is not None and best_d * self.scale < 20:
            if e.state & 0x0004:  # Ctrl
                self._selected.symmetric_difference_update({best})
            else:
                self._selected = {best}
        else:
            self._selected.clear()
        self._apply_selection()
        self.event_generate("<<FeatureSelected>>")

    # ------------------------------------------------------------ 选择
    def select(self, fids: set[str]):
        self._selected = set(fids)
        self._apply_selection()

    def selected(self) -> set[str]:
        return set(self._selected)

    def _apply_selection(self):
        c = self.canvas
        # 恢复全部特征项原色（line item 用 fill，无 outline 选项）
        for fid, entries in self._feat_items.items():
            for item, color in entries:
                c.itemconfigure(item, width=1, fill=color)
        # 高亮选中
        for fid in self._selected:
            for item, _color in self._feat_items.get(fid, []):
                c.itemconfigure(item, width=3, fill=C_HIGHLIGHT)
        # 覆盖区随特征选中联动换色（恢复深绿 → 选中亮黄；不触发 redraw）
        for _fid, item in self._cov_items.items():
            c.itemconfigure(item, fill=C_COVERAGE)
        for fid in self._selected:
            item = self._cov_items.get(fid)
            if item is not None:
                c.itemconfigure(item, fill=C_ORDER)
