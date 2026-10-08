# -*- coding: utf-8 -*-
"""左侧三视图（分组/列表/图层）+ 右侧参数面板。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from model import (Feature, Contour, FeatureParams, ToolLibrary, JobConfig,
                   THROUGH_HOLE, COUNTERBORE, DRILL, CIRCULAR_POCKET,
                   RECT_POCKET, POCKET, ON_VECTOR, OUTLINE, UNKNOWN,
                   SPIRAL, DIRECT_PLUNGE, OFFSET_POCKET, PARALLEL_POCKET,
                   CONTOUR, VECTOR_CUT)

STRATEGIES = [("螺旋下刀", SPIRAL), ("直下刀", DIRECT_PLUNGE),
              ("偏置清槽", OFFSET_POCKET), ("平行清槽", PARALLEL_POCKET),
              ("轮廓", CONTOUR), ("沿矢量加工", VECTOR_CUT)]

KIND_ORDER = {"": 99}


class LeftViews(ttk.Notebook):
    """三标签：分组 / 列表（左板件栏+右特征表）/ 图层。"""

    def __init__(self, master):
        super().__init__(master, width=320)
        # 分组树
        self.tree_group = ttk.Treeview(self, show="tree")
        self.add(self.tree_group, text=" 分组 ")
        # 列表 = 左侧板件栏 + 右侧特征表格（原"板件"独立标签页移入此处）
        frm = ttk.Frame(self)
        self.tree_board = ttk.Treeview(frm, show="tree", selectmode="browse")
        self.tree_board.column("#0", width=78)
        self.tree_board.pack(side=tk.LEFT, fill=tk.Y)
        right = ttk.Frame(frm)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        cols = ("kind", "center", "enabled", "tool", "depth", "order")
        self.tree_list = ttk.Treeview(right, columns=cols, show="headings")
        headings = {"kind": "类型", "center": "中心", "enabled": "启用",
                    "tool": "刀", "depth": "深", "order": "序"}
        widths = {"kind": 60, "center": 70, "enabled": 20,
                  "tool": 20, "depth": 36, "order": 26}
        for k in cols:
            self.tree_list.heading(k, text=headings[k])
            self.tree_list.column(k, width=widths[k], anchor=tk.W)
        self.tree_list.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        xs = ttk.Scrollbar(right, orient=tk.HORIZONTAL, command=self.tree_list.xview)
        xs.pack(side=tk.BOTTOM, fill=tk.X)
        self.tree_list.configure(xscrollcommand=xs.set)
        self.add(frm, text=" 列表 ")
        # 图层
        self.tree_layer = ttk.Treeview(self, show="tree")
        self.add(self.tree_layer, text=" 图层 ")

        # 序号调整：双击"序"列键入目标位置；右键菜单上移/下移/置首/置尾
        self.on_reorder = None      # 回调 (fid, action, value)
        self.on_regroup = None      # 回调 (fid, new_group_key)：转移到其他分组
        self.on_reverse = None      # 回调 (fid)：开放链矢量反向
        self.on_board = None        # 回调 (board_index)：点列表页左侧板件切换活动板
        self._tip = None            # 板件悬浮 tooltip
        self._tip_after = None
        self.tree_board.bind("<ButtonRelease-1>", self._on_board_click)
        self.tree_board.bind("<Motion>", self._on_board_hover)
        self.tree_board.bind("<Leave>", self._cancel_tip)
        self.tree_list.bind("<Double-1>", self._on_dclick_order)
        self.tree_list.bind("<Button-3>", self._on_rclick_order)
        self.tree_group.bind("<Button-3>", self._on_rclick_group)

    def _on_dclick_order(self, ev):
        tr = self.tree_list
        if tr.identify_column(ev.x) != "#6":   # 第 6 列 = 序
            return
        fid = tr.identify_row(ev.y)
        if not fid or not self.on_reorder:
            return
        cur = tr.set(fid, "order") or "?"
        from tkinter import simpledialog
        n = simpledialog.askinteger(
            "调整序号", f"移动到第几号？（当前 {cur}）\n其余特征自动顺延",
            parent=self, minvalue=1, maxvalue=99999)
        if n:
            self.on_reorder(fid, "set", n)

    def _on_rclick_order(self, ev):
        tr = self.tree_list
        fid = tr.identify_row(ev.y)
        if not fid:
            return
        tr.selection_set(fid)
        tr.focus(fid)
        m = tk.Menu(self, tearoff=0)
        if self.on_reorder:
            for label, act in (("上移", "up"), ("下移", "down"),
                               ("置首", "first"), ("置尾", "last")):
                m.add_command(label=label,
                              command=lambda a=act: self.on_reorder(fid, a, None))
        try:
            m.tk_popup(ev.x_root, ev.y_root)
        finally:
            m.grab_release()

    def _on_rclick_group(self, ev):
        """分组选项卡：子项右键 → 转移到其他分组。"""
        if not self.on_regroup:
            return
        fid = self.tree_group.identify_row(ev.y)
        fids = {f.id for f in getattr(self, "_features", [])}
        if not fid or fid not in fids:
            return
        self.tree_group.selection_set(fid)
        f = next(f for f in self._features if f.id == fid)
        cur = f.group_key or f.kind
        m = tk.Menu(self, tearoff=0)
        # 开放链：矢量反向（画布箭头立即变向）
        closed = getattr(self, "_closed_of", {}).get(f.contour_id)
        if self.on_reverse and closed is False:
            m.add_command(label="矢量反向", command=lambda: self.on_reverse(fid))
            m.add_separator()
        sub = tk.Menu(m, tearoff=0)
        for g in getattr(self, "_group_names", []):
            if g and g != cur:
                sub.add_command(label=g, command=lambda gg=g: self.on_regroup(fid, gg))
        if sub.index("end") is None:
            return
        m.add_cascade(label="转移到分组", menu=sub)
        try:
            m.tk_popup(ev.x_root, ev.y_root)
        finally:
            m.grab_release()

    def _on_board_click(self, ev):
        row = self.tree_board.identify_row(ev.y)
        if row and self.on_board:
            self.on_board(int(row))

    # ------------------------------------------------------ 板件悬浮提示
    def _on_board_hover(self, ev):
        """板件栏悬浮 600ms 后显示完整文件名 tooltip（文件名过长时可见全名）。"""
        row = self.tree_board.identify_row(ev.y)
        if not row:
            self._cancel_tip()
            return
        if row == getattr(self, "_tip_row", None):
            return                       # 同一行：等待中或已显示
        self._cancel_tip()
        self._tip_row = row
        self._tip_after = self.after(600, lambda: self._show_tip(row))

    def _show_tip(self, row):
        if not self.tree_board.exists(row):
            self._cancel_tip()
            return
        self._tip = tw = tk.Toplevel(self)
        tw.wm_overrideredirect(True)     # 无边框小黄框
        tw.wm_geometry(f"+{self.winfo_pointerx() + 12}"
                       f"+{self.winfo_pointery() + 18}")
        ttk.Label(tw, text=self.tree_board.item(row, "text"),
                  background="#ffffe0", relief=tk.SOLID, borderwidth=1,
                  padding=(4, 2)).pack()

    def _cancel_tip(self, _e=None):
        self._tip_row = None
        if getattr(self, "_tip_after", None):
            self.after_cancel(self._tip_after)
            self._tip_after = None
        tip = getattr(self, "_tip", None)
        if tip is not None:
            tip.destroy()
            self._tip = None

    def fill_boards(self, names: list[str], active: int):
        tb = self.tree_board
        tb.delete(*tb.get_children(""))
        for i, n in enumerate(names):
            tb.insert("", "end", iid=str(i), text=n)
        if 0 <= active < len(names):
            tb.selection_set(str(active))

    # ---------------------------------------------------------- 填充
    def fill_features(self, features: list[Feature], contours: list[Contour],
                      all_features: list[Feature] | None = None,
                      board_of: dict[str, str] | None = None):
        """列表 = 当前活动板；分组 = 跨板聚合（批量作业时子项带 -板名）。"""
        by_id = {c.id: c for c in contours}
        grp_src = all_features if all_features is not None else features
        self._features = grp_src      # 跨板集合（分组右键/转移分组用）
        self._group_names = sorted({f.group_key or f.kind for f in grp_src})
        # 分组（跨板：同几何签名聚一组，子项标注所属板）
        tg = self.tree_group
        tg.delete(*tg.get_children(""))
        groups: dict[str, list[Feature]] = {}
        for f in grp_src:
            groups.setdefault(f.group_key or f.kind, []).append(f)
        for gname in sorted(groups):
            gfs = groups[gname]
            parent = tg.insert("", "end", text=f"{gname} ({len(gfs)})",
                               open=False, values=())
            for f in gfs:
                label = self._row_label(f, by_id)
                if board_of and len({board_of.get(x.id) for x in gfs}) > 1:
                    label = f"{label}-{board_of.get(f.id, '')}"
                tg.insert(parent, "end", iid=f.id, text=label,
                          values=(f.id,), tags=(f.id,))
        # 列表（右侧特征表；启用列 √/空）
        tl = self.tree_list
        tl.delete(*tl.get_children(""))
        for f in sorted(features, key=lambda x: x.order or 999):
            c = by_id.get(f.contour_id)
            ctr = f"({c.centroid[0]:.0f},{c.centroid[1]:.0f})" if c else ""
            tl.insert("", "end", iid=f.id,
                      values=(f.kind_cn(), ctr,
                              "√" if f.params.enabled else "",
                              f.params.tool_id,
                              f"{f.params.depth:g}" if f.params.depth else "!",
                              f.order or ""),
                      tags=(f.id,))

    def fill_layers(self, layer_roles: dict[str, str], counts: dict[str, int]):
        tl = self.tree_layer
        tl.delete(*tl.get_children(""))
        role_cn = {"drill": "钻孔", "tool": "刀号", "section": "剖面",
                   "ignore": "忽略", "geom": "几何", "zero": "图层0",
                   "island": "避让岛"}
        for name in sorted(layer_roles):
            r = layer_roles[name]
            mark = " [忽略]" if r == "ignore" else ""
            tl.insert("", "end",
                      text=f"{name} ({counts.get(name, 0)}) → {role_cn.get(r, r)}{mark}")

    @staticmethod
    def _row_label(f: Feature, by_id) -> str:
        return f.label or f.id

    # ---------------------------------------------------------- 同步
    def selected_feature_ids(self) -> set[str]:
        out = set()
        for tree in (self.tree_group, self.tree_list):
            for iid in tree.selection():
                if tree.get_children(iid):
                    # 分组父节点：选全部子节点
                    for child in tree.get_children(iid):
                        out.add(child)
                else:
                    out.add(iid)   # 特征行（id 带板名前缀也正确）
        return out

    def show_selection(self, fids: set[str]):
        for tree in (self.tree_group, self.tree_list):
            tree.selection_set([i for i in fids if tree.exists(i)])
            # 展开分组父节点
            for parent in self.tree_group.get_children(""):
                for child in self.tree_group.get_children(parent):
                    if child in fids:
                        self.tree_group.item(parent, open=True)
                        break


class ParamsPanel(ttk.Frame):
    """右侧参数面板：当前选中（多选时批量写）。"""

    def __init__(self, master, lib: ToolLibrary, cfg: JobConfig):
        super().__init__(master, padding=8)
        self.lib = lib
        self.cfg = cfg
        self.features: list[Feature] = []

        ttk.Label(self, text="参数（选中特征）", font=("Segoe UI", 10, "bold")
                  ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 6))

        def row(r, label):
            lab = ttk.Label(self, text=label)
            lab.grid(row=r, column=0, sticky=tk.W, pady=2)
            w = ttk.Frame(self)
            w.grid(row=r, column=1, sticky=tk.EW, pady=2)
            w._label = lab
            return w

        # 刀具
        w = row(1, "刀具")
        self.var_tool = tk.StringVar()
        self.cb_tool = ttk.Combobox(w, textvariable=self.var_tool, state="readonly",
                                    values=list(self.lib.tools.keys()), width=8)
        self.cb_tool.pack(side=tk.LEFT)
        # 深度
        w = row(2, "总深度 mm")
        self.ent_depth = ttk.Entry(w, width=10)
        self.ent_depth.pack(side=tk.LEFT)
        # 分刀
        w = row(3, "分刀")
        self.var_pass_mode = tk.StringVar(value="passes")
        rb1 = ttk.Radiobutton(w, text="次数", variable=self.var_pass_mode,
                              value="passes")
        self.ent_passes = ttk.Entry(w, width=4)
        rb2 = ttk.Radiobutton(w, text="步距", variable=self.var_pass_mode,
                              value="step")
        self.ent_stepdown = ttk.Entry(w, width=6)
        rb1.pack(side=tk.LEFT)
        self.ent_passes.pack(side=tk.LEFT, padx=(2, 6))
        rb2.pack(side=tk.LEFT)
        self.ent_stepdown.pack(side=tk.LEFT, padx=2)
        # 策略
        w = row(4, "策略")
        self.var_strategy = tk.StringVar()
        self.cb_strategy = ttk.Combobox(w, textvariable=self.var_strategy,
                                        state="readonly",
                                        values=[s[0] for s in STRATEGIES], width=10)
        self.cb_strategy.pack(side=tk.LEFT)
        self.cb_strategy.bind("<<ComboboxSelected>>",
                              lambda _e: self._update_strategy_rows())
        # 行距（偏置/平行清槽时显示）
        w = row(5, "行距")
        self._row_step = w
        self.var_step_mode = tk.StringVar(value="90%")
        cb3 = ttk.Combobox(w, textvariable=self.var_step_mode, state="readonly",
                           values=["50%", "60%", "70%", "80%", "90%", "自定义"],
                           width=6)
        cb3.pack(side=tk.LEFT)
        cb3.bind("<<ComboboxSelected>>", lambda _e: self._update_strategy_rows())
        self.ent_step_mm = ttk.Entry(w, width=6)
        self.ent_step_mm.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(w, text="mm").pack(side=tk.LEFT)
        self._lbl_step_mm = w.winfo_children()[-1]
        # 平行方向（平行清槽时显示）
        w = row(6, "平行方向")
        self._row_axis = w
        self.var_axis = tk.StringVar(value="X")
        ttk.Combobox(w, textvariable=self.var_axis, state="readonly",
                     values=["X", "Y"], width=4).pack(side=tk.LEFT)
        # 起刀方向（偏置清槽时显示：外→内 / 内→外）
        w = row(7, "起刀方向")
        self._row_pdir = w
        self.var_pdir = tk.StringVar(value="从外到内")
        ttk.Combobox(w, textvariable=self.var_pdir, state="readonly",
                     values=["从外到内", "从内到外"], width=8).pack(side=tk.LEFT)
        # 刀位（沿矢量加工时显示：刀心在矢量左/右/居中）
        w = row(8, "刀位")
        self._row_side = w
        self.var_side = tk.StringVar(value="居中")
        ttk.Combobox(w, textvariable=self.var_side, state="readonly",
                     values=["矢量左侧", "矢量右侧", "居中"], width=8).pack(side=tk.LEFT)
        # 启用
        w = row(9, "启用")
        self.var_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(w, text="生成刀路", variable=self.var_enabled).pack(side=tk.LEFT)
        # 信息
        self.info = tk.StringVar(value="未选中")
        ttk.Label(self, textvariable=self.info, foreground="#447",
                  wraplength=220, justify=tk.LEFT).grid(
            row=10, column=0, columnspan=2, sticky=tk.W, pady=(10, 2))
        # 警告
        self.warn = tk.StringVar()
        ttk.Label(self, textvariable=self.warn, foreground="#b33",
                  wraplength=220, justify=tk.LEFT).grid(
            row=11, column=0, columnspan=2, sticky=tk.W)
        # 按钮
        bar = ttk.Frame(self)
        bar.grid(row=12, column=0, columnspan=2, sticky=tk.EW, pady=(10, 0))
        self.btn_apply = ttk.Button(bar, text="应用到选中", command=self._apply)
        self.btn_apply_group = ttk.Button(bar, text="应用到全组",
                                          command=self._apply_group)
        self.btn_apply.pack(side=tk.LEFT, padx=(0, 4))
        self.btn_apply_group.pack(side=tk.LEFT)
        self.columnconfigure(1, weight=1)

        self.on_change = None          # 回调：参数变化后刷新
        self._closed_of: dict[str, bool] = {}   # contour_id → 是否闭合（App 注入，跨板）
        self._update_strategy_rows()

    # ---------------------------------------------------------- 行距/方向显隐
    def _update_strategy_rows(self):
        code = dict(STRATEGIES).get(self.var_strategy.get(), SPIRAL)
        pocket = code in (OFFSET_POCKET, PARALLEL_POCKET)
        if pocket:
            self._row_step.grid(); self._row_step._label.grid()
            if code == PARALLEL_POCKET:
                self._row_axis.grid(); self._row_axis._label.grid()
            else:
                self._row_axis.grid_remove(); self._row_axis._label.grid_remove()
            if code == OFFSET_POCKET:
                self._row_pdir.grid(); self._row_pdir._label.grid()
            else:
                self._row_pdir.grid_remove(); self._row_pdir._label.grid_remove()
        else:
            self._row_step.grid_remove(); self._row_step._label.grid_remove()
            self._row_axis.grid_remove(); self._row_axis._label.grid_remove()
            self._row_pdir.grid_remove(); self._row_pdir._label.grid_remove()
        if code == VECTOR_CUT:
            self._row_side.grid(); self._row_side._label.grid()
        else:
            self._row_side.grid_remove(); self._row_side._label.grid_remove()
        custom = self.var_step_mode.get() == "自定义"
        self.ent_step_mm.configure(state=tk.NORMAL if custom else tk.DISABLED)
        self._lbl_step_mm.configure(
            foreground="#000" if custom else "#aaa")

    # ---------------------------------------------------------- 显示
    def show(self, features: list[Feature]):
        self.features = features
        if not features:
            self.info.set("未选中")
            self.warn.set("")
            self.cb_strategy.configure(values=[s[0] for s in STRATEGIES])
            return
        f = features[0]
        p = f.params
        # 沿矢量加工仅对开放链特征开放
        open_chain = any(self._closed_of.get(fs.contour_id) is False
                         for fs in features)
        self.cb_strategy.configure(values=[
            s[0] for s in STRATEGIES if open_chain or s[1] != VECTOR_CUT])
        self.var_tool.set(p.tool_id)
        self.ent_depth.delete(0, tk.END)
        self.ent_depth.insert(0, f"{p.depth:g}" if p.depth else "")
        if p.n_passes:
            self.var_pass_mode.set("passes")
            self.ent_passes.delete(0, tk.END)
            self.ent_passes.insert(0, str(p.n_passes))
        else:
            self.var_pass_mode.set("step")
            self.ent_stepdown.delete(0, tk.END)
            self.ent_stepdown.insert(0, f"{p.stepdown:g}" if p.stepdown else "")
        for cn, code in STRATEGIES:
            if code == p.strategy:
                self.var_strategy.set(cn)
                break
        else:
            self.var_strategy.set(STRATEGIES[0][0])
        # 行距 / 平行方向回填
        if p.stepover_mm:
            self.var_step_mode.set("自定义")
            self.ent_step_mm.delete(0, tk.END)
            self.ent_step_mm.insert(0, f"{p.stepover_mm:g}")
        else:
            pct = round(p.stepover_factor * 100)
            if pct in (50, 60, 70, 80, 90):
                self.var_step_mode.set(f"{pct}%")
            else:   # 非标准系数：折算为等效 mm 回填
                t = self.lib.tools.get(p.tool_id)
                mm = p.stepover_factor * t.diameter if t else 0.0
                self.var_step_mode.set("自定义")
                self.ent_step_mm.delete(0, tk.END)
                self.ent_step_mm.insert(0, f"{mm:g}" if mm else "")
        self.var_axis.set(p.parallel_axis or "X")
        self.var_pdir.set("从内到外" if p.pocket_direction == "inside_out"
                          else "从外到内")
        self.var_side.set({"left": "矢量左侧", "right": "矢量右侧"}.get(
            p.vector_side, "居中"))
        self._update_strategy_rows()
        self.var_enabled.set(p.enabled)
        multi = f"（{len(features)} 项，批量为首项参数）" if len(features) > 1 else ""
        self.info.set(f"{f.kind_cn()} {f.label} {multi}")
        self.warn.set("\n".join(f.warnings[:3]))

    # ---------------------------------------------------------- 应用
    def refresh_tools(self):
        """刀具库变化后刷新下拉（保留当前选择，失效则回 T1）。"""
        cur = self.var_tool.get()
        self.cb_tool.configure(values=list(self.lib.tools.keys()))
        self.var_tool.set(cur if cur in self.lib.tools else
                          (next(iter(self.lib.tools), "T1")))

    def _collect(self) -> FeatureParams | None:
        try:
            depth = float(self.ent_depth.get() or 0)
        except ValueError:
            self.warn.set("深度不是数字")
            return None
        n_passes = None
        stepdown = 0.0
        if self.var_pass_mode.get() == "passes":
            try:
                n_passes = int(self.ent_passes.get() or 1)
            except ValueError:
                n_passes = 1
        else:
            try:
                stepdown = float(self.ent_stepdown.get() or 0)
            except ValueError:
                stepdown = 0.0
        strategy = dict(STRATEGIES).get(self.var_strategy.get(), SPIRAL)
        sm = self.var_step_mode.get()
        if sm == "自定义":
            try:
                v = float(self.ent_step_mm.get() or 0)
                step_mm = v if v > 0 else None
            except ValueError:
                step_mm = None
            step_factor = 0.9
        else:
            step_mm = None
            step_factor = int(sm[:-1]) / 100.0
        return FeatureParams(tool_id=self.var_tool.get(), depth=depth,
                             n_passes=n_passes, stepdown=stepdown,
                             strategy=strategy, enabled=self.var_enabled.get(),
                             stepover_factor=step_factor, stepover_mm=step_mm,
                             parallel_axis=self.var_axis.get(),
                             pocket_direction=("inside_out"
                                               if self.var_pdir.get() == "从内到外"
                                               else "outside_in"),
                             vector_side={"矢量左侧": "left", "矢量右侧": "right"}.get(
                                 self.var_side.get(), "center"))

    def _apply(self):
        p = self._collect()
        if p is None or not self.features:
            return
        for f in self.features:
            f.params.tool_id = p.tool_id
            f.params.depth = p.depth
            f.params.n_passes = p.n_passes
            f.params.stepdown = p.stepdown
            f.params.strategy = p.strategy
            f.params.enabled = p.enabled
            f.params.stepover_factor = p.stepover_factor
            f.params.stepover_mm = p.stepover_mm
            f.params.parallel_axis = p.parallel_axis
            f.params.pocket_direction = p.pocket_direction
            f.params.vector_side = p.vector_side
        if self.on_change:
            self.on_change()

    def _apply_group(self):
        if not self.features:
            return
        groups = {f.group_key for f in self.features}
        p = self._collect()
        if p is None:
            return
        # 找同组全部
        all_fs = getattr(self, "_all_features", [])
        for f in all_fs:
            if f.group_key in groups:
                f.params.tool_id = p.tool_id
                f.params.depth = p.depth
                f.params.n_passes = p.n_passes
                f.params.stepdown = p.stepdown
                f.params.strategy = p.strategy
                f.params.enabled = p.enabled
                f.params.stepover_factor = p.stepover_factor
                f.params.stepover_mm = p.stepover_mm
                f.params.parallel_axis = p.parallel_axis
                f.params.vector_side = p.vector_side
        if self.on_change:
            self.on_change()
