# -*- coding: utf-8 -*-
"""DXF→CNC 刀路生成器 主程序。

启动：python cnc_gui.py
链路：DXF → 识别(规则) → 排序(绕板) → 刀路(螺旋/偏置) → 宝元 .cnc（单刀单文件）
"""
from __future__ import annotations

import json
import math
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from dataclasses import asdict

from cam import CamJob
from canvas_view import CanvasView
from panels import LeftViews, ParamsPanel, STRATEGIES
import toolpath
from model import (JobConfig, Tool, ToolLibrary, Feature, Move, THROUGH_HOLE,
                   DRILL, COUNTERBORE, RECT_POCKET, POCKET, CIRCULAR_POCKET,
                   SPIRAL, VECTOR_CUT, OFFSET_POCKET, PARALLEL_POCKET)
from io_dxf import ROLE_IGNORE
from recognize import Rules, HoleRule, LayerDefault

APP_TITLE = "DXF→CNC 刀路生成器（平面多层板 2.5D）"
ROOT_DIR = Path(__file__).resolve().parent
CONFIG = ROOT_DIR / "config"


# ----------------------------------------------------------------------------
# 批量作业：一块板 = 一个 DXF 的识别结果 + 排序
# ----------------------------------------------------------------------------

class _EmptyDxfData:
    """过程文件板的空 DXF 数据壳。"""
    layer_roles: dict = {}
    entities: list = []
    ignored: list = []


class Board:
    def __init__(self, name: str, path: str, result, ordered: list,
                 data=None, layer_counts: dict | None = None,
                 ignored_objs: list | None = None,
                 moves_by_tool: dict | None = None,
                 feature_moves: dict | None = None):
        self.name = name
        self.path = path
        self.result = result
        self.ordered = ordered
        self.data = data if data is not None else _EmptyDxfData()
        self.layer_counts = layer_counts or {}
        # 忽略层显示对象列表（画布"忽略层"开关用）：
        # {"kind": "polyline"/"circle"/"text"/"point", ...}
        self.ignored_objs = ignored_objs or []
        # 刀路快照 {tool_id: [Move]}（baoyuan 恢复后画布可复查刀轨）
        self.moves_by_tool = moves_by_tool or {}
        # 按特征分组刀路快照 {feature_id: [Move]}（覆盖区点选高亮复查一致性）
        self.feature_moves = feature_moves or {}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1280x800")

        # 配置加载（config 存在则用）
        cfg = JobConfig()
        rules = Rules.load(CONFIG / "rules.json") if (CONFIG / "rules.json").exists() else Rules()
        lib = ToolLibrary()
        if (CONFIG / "tools.json").exists():
            try:
                lib = ToolLibrary(Tool.load_library(CONFIG / "tools.json"))
            except Exception:
                pass
        self.job = CamJob(cfg, rules, lib)
        self._sort_var = tk.StringVar(value="绕板默认")

        self._build_toolbar()
        self._build_main()
        self._build_statusbar()

        self.protocol("WM_DELETE_WINDOW", self.destroy)

    # ---------------------------------------------------------------- 工具栏
    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(6, 4))
        bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(bar, text="打开 DXF", command=self.open_dxf).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="生成刀路", command=self.gen_toolpath_preview).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="生成 CNC", command=self.export_cnc).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="批量导出", command=self.export_batch).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="智能排序", command=self.do_sort).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="刀具库", command=self.open_tools).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="规则集", command=self.open_rules).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="关联.baoyuan双击", command=self.register_baoyuan).pack(
            side=tk.LEFT, padx=2)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        # 排序策略
        ttk.Label(bar, text="排序:").pack(side=tk.LEFT)
        self._sort_names = {"绕板默认": "around", "按刀具分组": "by_tool",
                            "就近最少空跑": "nearest", "手动锁定": "manual"}
        self._sort_var = tk.StringVar(value="绕板默认")
        cb = ttk.Combobox(bar, textvariable=self._sort_var, state="readonly",
                          width=10, values=list(self._sort_names.keys()))
        cb.pack(side=tk.LEFT, padx=2)
        # 板厚 / 回零
        ttk.Label(bar, text="板厚:").pack(side=tk.LEFT, padx=(10, 0))
        self.ent_thick = ttk.Entry(bar, width=5)
        self.ent_thick.insert(0, str(self.job.cfg.stock_thickness))
        self.ent_thick.pack(side=tk.LEFT)
        ttk.Label(bar, text="回零Y:").pack(side=tk.LEFT, padx=(10, 0))
        self.ent_home = ttk.Entry(bar, width=6)
        self.ent_home.insert(0, str(self.job.cfg.home[1]))
        self.ent_home.pack(side=tk.LEFT)
        ttk.Button(bar, text="适应视图", command=lambda: self.view.fit_view()
                   ).pack(side=tk.LEFT, padx=(10, 2))

    # ---------------------------------------------------------------- 主体
    def _build_main(self):
        main = ttk.Frame(self)
        main.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # 左：三视图（真实点击绑定：本机环境虚拟事件 <<TreeviewSelect>> 不可靠）
        self.views = LeftViews(main)
        self.views.pack(side=tk.LEFT, fill=tk.Y, padx=(4, 2), pady=2)
        for tree in (self.views.tree_group, self.views.tree_list):
            tree.bind("<ButtonRelease-1>", self._on_list_select)
            tree.bind("<<TreeviewSelect>>", self._on_list_select)  # 双保险
            tree.bind("<KeyRelease>", self._on_list_select)        # 键盘上下键

        # 中：画布
        self.view = CanvasView(main)
        self.view.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2, pady=2)
        self.view.bind("<<FeatureSelected>>", self._on_canvas_select)

        # 画布显示开关条
        sw = ttk.Frame(self.view)
        sw.place(relx=1.0, x=-6, y=6, anchor=tk.NE)
        for text, var in (("几何", self.view.show_geom), ("忽略层", self.view.show_ignored),
                          ("顺序", self.view.show_order), ("刀路", self.view.show_toolpath),
                          ("覆盖区", self.view.show_coverage)):
            ttk.Checkbutton(sw, text=text, variable=var,
                            command=self.view.redraw).pack(side=tk.LEFT, padx=2)

        # 右：参数
        self.params = ParamsPanel(main, self.job.lib, self.job.cfg)
        self.params.pack(side=tk.LEFT, fill=tk.Y, padx=(2, 4), pady=2)
        self.params.on_change = self._refresh_views
        self.views.on_reorder = self.reorder_feature
        self.views.on_regroup = self._regroup_feature
        self.views.on_reverse = self._reverse_vector
        self.views.on_board = self._set_active
        self.boards: list[Board] = []
        self.active_idx = -1

    def _build_statusbar(self):
        self.status = tk.StringVar(value="请打开 DXF 文件")
        bar = ttk.Label(self, textvariable=self.status, relief=tk.SUNKEN,
                        anchor=tk.W, padding=(8, 3))
        bar.pack(side=tk.BOTTOM, fill=tk.X)

    # ---------------------------------------------------------------- 动作
    def _read_stock_inputs(self):
        """读取用户改的板厚/回零。"""
        try:
            self.job.cfg.stock_thickness = float(self.ent_thick.get())
        except ValueError:
            pass
        try:
            self.job.cfg.home = (0.0, float(self.ent_home.get()))
        except ValueError:
            pass

    def _load_one_board(self, path: str) -> Board:
        res = self.job.load(path)
        # 跨板编号：特征/轮廓 id 加"板名-"前缀，避免多板 F000 冲突（中文板名合法）
        prefix = Path(path).stem + "-"
        cmap = {c.id: prefix + c.id for c in res.contours}
        for c in res.contours:
            c.id = cmap[c.id]
        for f in res.features:
            f.id = prefix + f.id
            f.contour_id = cmap.get(f.contour_id, f.contour_id)
            f.paired_ids = [cmap.get(x, x) for x in f.paired_ids]
        counts: dict[str, int] = {}
        for e in self.job.data.entities + self.job.data.ignored:
            counts[e.layer] = counts.get(e.layer, 0) + 1
        # 忽略层显示对象：折线（LINE/ARC/多段线）、圆、以及 DIMENSION/HATCH/
        # TEXT 等参考实体在 io_dxf 展开好的渲染子对象（尺寸线/标注文字/剖面线）
        ignored_objs: list[dict] = []
        for e in self.job.data.ignored:
            if e.circle is not None:
                ignored_objs.append({"kind": "circle", "circle": e.circle})
            elif len(e.points) >= 2:
                ignored_objs.append({"kind": "polyline", "pts": e.points})
            ignored_objs.extend(getattr(e, "render", []))
        return Board(Path(path).stem, str(path), res, list(self.job.ordered),
                     self.job.data, counts, ignored_objs)

    def gen_toolpath_preview(self):
        """生成刀路预览：只算当前活动板，结果缓存进 Board（切板不丢）。"""
        if not (0 <= self.active_idx < len(self.boards)):
            messagebox.showinfo("提示", "请先导入 DXF / 过程文件")
            return
        b = self.boards[self.active_idx]
        self.job.result = b.result
        self.job.ordered = b.ordered
        b.moves_by_tool = self.job.moves_by_tool()
        b.feature_moves = self.job.moves_by_feature()
        b.ordered = self.job.ordered       # moves_by_tool 内部可能自动排序，存回板
        if b.moves_by_tool:
            self.view.set_toolpath(next(iter(b.moves_by_tool.values())))
        self._update_coverage(b.moves_by_tool, b.feature_moves)
        if b.moves_by_tool:
            n_seg = sum(len(v) for v in b.moves_by_tool.values())
            self.status.set(f"已生成刀路预览：{b.name} / "
                            f"{len(b.moves_by_tool)} 把刀 / {n_seg} 段")
        else:
            self.status.set(f"未生成刀路：{b.name}（检查特征深度/刀具设置）")

    def open_dxf(self):
        """打开 DXF / 过程文件：一次可选 1 个或多个文件（.dxf 与 .baoyuan 可混选）。"""
        paths = filedialog.askopenfilenames(
            title="打开 DXF / 过程文件（可多选，每文件一块板）",
            filetypes=[("DXF 或过程文件", "*.dxf *.baoyuan"),
                       ("DXF 图纸", "*.dxf"), ("过程文件", "*.baoyuan"),
                       ("所有文件", "*.*")])
        if not paths:
            return
        # 单个 .baoyuan：完整恢复（含全局配置/刀具库/排序模式）
        if len(paths) == 1 and paths[0].lower().endswith(".baoyuan"):
            self._load_baoyuan(paths[0])
            return
        self._read_stock_inputs()
        boards: list[Board] = []
        errs: list[str] = []
        for p in paths:
            try:
                if p.lower().endswith(".baoyuan"):
                    boards.append(self._board_from_baoyuan(p, restore_globals=False))
                else:
                    boards.append(self._load_one_board(p))
            except Exception as e:  # noqa: BLE001
                errs.append(f"{Path(p).name}: {e}")
        if errs:
            messagebox.showwarning("部分文件导入失败", "\n".join(errs[:6]))
        if not boards:
            return
        self.boards = boards
        self.active_idx = -1
        self._set_active(0, sort_now=True)
        n_feat = sum(len(b.result.features) for b in boards)
        self.status.set(f"已导入 {len(boards)} 板 / {n_feat} 特征："
                        + "、".join(b.name for b in boards[:6])
                        + ("…" if len(boards) > 6 else ""))

    def _set_active(self, i: int, sort_now: bool = False):
        """切换活动板：视图/参数全部切换；跨板"应用到全组"生效。"""
        if not (0 <= i < len(self.boards)):
            return
        if 0 <= self.active_idx < len(self.boards):   # 存回当前板排序
            self.boards[self.active_idx].ordered = self.job.ordered
        self.active_idx = i
        b = self.boards[i]
        self.job.result = b.result
        self.job.ordered = b.ordered
        self.job.data = b.data
        self._dxf_path = b.path
        self.views.fill_layers(b.data.layer_roles, b.layer_counts)
        self.view.set_data(b.result.contours, b.result.features, b.ignored_objs)
        if sort_now or not b.ordered:
            self.do_sort()
        else:
            self._refresh_views(invalidate=False)   # 切板仅刷新显示，不丢刀路缓存
        # 恢复该板刀路预览与覆盖区（须在 do_sort/_refresh_views 之后，
        # 两者都会清空画布 moves）。仅显示板缓存（"生成刀路"按钮 / 导出 /
        # baoyuan 快照写入），缓存空则画布无刀路——不懒加载现算。
        if b.moves_by_tool:
            self.view.set_toolpath(next(iter(b.moves_by_tool.values())))
        self._update_coverage(b.moves_by_tool, b.feature_moves)
        # "应用到全组"跨板：同名分组所有板一起改
        self.params._all_features = [f for bb in self.boards
                                     for f in bb.result.features]
        self.params.show([])
        self.views.fill_boards([bb.name for bb in self.boards], i)

    def _update_coverage(self, moves_by_tool: dict, feature_moves: dict):
        """覆盖区计算与送显：全部刀 FEED 段按各自刀径缓冲并集（统一深绿，
        不按刀分色）。每特征另算分区多边形，供点选高亮联动。

        moves_by_tool / feature_moves 来自板缓存（baoyuan 快照）或现算；
        刀径从刀具库按刀取——不同刀直径不同，禁止用第一把刀刀径统一算。
        """
        from shapely.ops import unary_union
        polys = []
        for tid, lst in (moves_by_tool or {}).items():
            try:
                dia = self.job.lib.get(tid).diameter
            except KeyError:
                continue          # 未知刀具无刀径，跳过该刀
            pg = toolpath.covered_area(lst, dia)
            if pg is not None:
                polys.append(pg)
        self.view.set_coverage(unary_union(polys) if polys else None)
        # 特征覆盖区：fid → 该特征全部 FEED 段沿其刀具刀径的并集
        by_fid = {f.id: f for f in self.job.result.features} \
            if self.job.result else {}
        fcov: dict = {}
        for fid, mv in (feature_moves or {}).items():
            f = by_fid.get(fid)
            if f is None:
                continue
            try:
                dia = self.job.lib.get(f.params.tool_id).diameter
            except KeyError:
                continue
            pg = toolpath.covered_area(mv, dia)
            if pg is not None:
                fcov[fid] = pg
        self.view.set_feature_coverage(fcov)

    def do_sort(self):
        if not self.job.result:
            return
        strategy = self._sort_names.get(self._sort_var.get(), "around")
        self.job.sort(strategy)
        self._refresh_views()
        n = sum(1 for f in self.job.ordered if f.params.enabled)
        self.status.set(f"排序完成：{n} 个启用特征（{self._sort_var.get()}）")

    def reorder_feature(self, fid: str, action: str, value):
        """手动调整顺序：set=移动到第 value 位；up/down/first/last。
        其余特征自动顺延，序号压缩为连续 1..N；切到"手动锁定"模式。"""
        lst = list(self.job.ordered)
        ids = [f.id for f in lst]
        if fid not in ids:
            return
        idx = ids.index(fid)
        f = lst.pop(idx)
        if action == "set":
            n = max(1, min(int(value), len(lst) + 1))
            lst.insert(n - 1, f)
        elif action == "up":
            lst.insert(max(0, idx - 1), f)
        elif action == "down":
            lst.insert(min(len(lst), idx + 1), f)
        elif action == "first":
            lst.insert(0, f)
        elif action == "last":
            lst.append(f)
        else:
            lst.insert(idx, f)
        for i, ff in enumerate(lst):
            ff.order = i + 1
        self.job.ordered = lst
        self._sort_var.set("手动锁定")
        self._refresh_views()
        self.status.set(f"手动排序：{f.kind_cn()} → 第 {f.order} 号")

    def _regroup_feature(self, fid: str, new_group: str):
        """把特征转移到其他分组（识别分组不对时手动归类）。"""
        if not self.job.result:
            return
        for f in self.job.result.features:
            if f.id == fid:
                f.group_key = new_group
                self._refresh_views()
                self.status.set(f"{f.label} 已转移到分组「{new_group}」")
                return

    # ------------------------------------------------------------ 过程文件
    def _save_baoyuan(self, out_dir: Path, basename: str, dxf_path: str | None = None,
                      board: Board | None = None, moves_map: dict | None = None):
        """导出 CNC 时同步保存过程文件（几何快照 + 全部参数 + 忽略层 + 刀路，可复查/复现）。

        board 必传：忽略层/图层角色显式从 Board 取。禁止读 self.job.data ——
        批量导出循环里 job.data 仍指向上一块板（未随 result/ordered 切换）。
        moves_map 未传时用 job.moves_by_tool() 现算（此时 job.result/ordered
        已切到 board，结果正确）。
        """
        import json
        from datetime import datetime
        snap = {
            "format": "baoyuan-process/1",
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "dxf_path": dxf_path or getattr(self, "_dxf_path", ""),
            "tools": [asdict(t) for t in self.job.lib.tools.values()],
            "job_cfg": asdict(self.job.cfg),
            "sort_mode": self._sort_names.get(self._sort_var.get(), "around"),
            "contours": [asdict(c) for c in self.job.result.contours],
            "stock_id": self.job.result.stock.id if self.job.result.stock else "",
            "features": [asdict(f) for f in self.job.result.features],
        }
        if board is not None:
            # 忽略层显示对象 / 图层角色与计数 / 刀路快照（tuple 经 json 自动转 list，
            # 画布按索引访问，list 与 tuple 兼容，加载侧无需转回）
            snap["layer_roles"] = dict(board.data.layer_roles)
            snap["layer_counts"] = dict(board.layer_counts)
            snap["ignored_objs"] = board.ignored_objs
            m = moves_map if moves_map is not None else self.job.moves_by_tool()
            snap["moves_by_tool"] = {tid: [asdict(mv) for mv in lst]
                                     for tid, lst in m.items()}
            # 按特征分组刀路（覆盖区点选高亮复查与导出时一致；此处 job.result
            # 已是 board 的，与 moves_by_tool 循环结果一致）
            fm = self.job.moves_by_feature()
            snap["feature_moves"] = {fid: [asdict(mv) for mv in lst]
                                     for fid, lst in fm.items()}
        p = out_dir / f"{basename}.baoyuan"
        p.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
        return p

    def _board_from_baoyuan(self, path: str, restore_globals: bool = True) -> Board:
        """从 .baoyuan 过程文件构建 Board（几何快照内嵌，原 DXF 删除也能复现）。

        restore_globals=True 时恢复全局配置/刀具库/排序模式（单文件打开用）；
        多文件混选时传 False，避免反复覆盖当前会话配置。
        """
        from model import Contour, Feature, FeatureParams
        from recognize import RecognitionResult
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        contours = []
        for d in data["contours"]:
            d["pts"] = [(x, y) for x, y in d["pts"]]
            d["centroid"] = tuple(d["centroid"])
            d["bbox"] = tuple(d["bbox"])
            contours.append(Contour(**d))
        features = []
        for d in data["features"]:
            pd = d.pop("params")
            features.append(Feature(**d, params=FeatureParams(**pd)))
        if restore_globals:
            tools = [Tool(**t) for t in data.get("tools", [])]
            self.job.cfg = JobConfig(**data.get("job_cfg", {}))
            if tools:
                self.job.lib = ToolLibrary(tools)
            mode = data.get("sort_mode", "手动锁定")
            for cn, key in self._sort_names.items():
                if key == mode or cn == mode:
                    self._sort_var.set(cn)
                    break
            else:
                self._sort_var.set("手动锁定")
        stock_id = data.get("stock_id", "")
        stock = next((c for c in contours if c.id == stock_id), None)
        result = RecognitionResult(contours=contours, features=features,
                                   stock=stock,
                                   warnings=data.get("warnings", []))
        ordered = sorted(features, key=lambda f: f.order)
        # 恢复忽略层显示对象 / 图层角色与计数 / 刀路快照
        # （旧格式无这些字段 → 空默认，行为与现状一致）
        moves_by_tool = {tid: [Move(**m) for m in lst]
                         for tid, lst in data.get("moves_by_tool", {}).items()}
        feature_moves = {fid: [Move(**m) for m in lst]
                         for fid, lst in data.get("feature_moves", {}).items()}
        board = Board(Path(path).stem, data.get("dxf_path", ""), result, ordered,
                      layer_counts=data.get("layer_counts", {}),
                      ignored_objs=data.get("ignored_objs", []),
                      moves_by_tool=moves_by_tool,
                      feature_moves=feature_moves)
        board.data.layer_roles = data.get("layer_roles", {})
        return board

    def _load_baoyuan(self, path: str):
        """打开 .baoyuan 过程文件：完整恢复几何 + 全部加工参数。"""
        try:
            board = self._board_from_baoyuan(path, restore_globals=True)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("过程文件打开失败", str(e))
            return
        self.boards = [board]
        self.active_idx = -1
        self.params.refresh_tools()
        self._set_active(0)
        src = board.path
        self.status.set(f"过程文件已载入：{Path(path).name}（源 {Path(src).name if src else '未记录'}，"
                        f"{len(board.result.features)} 特征 / "
                        f"{len(board.result.contours)} 轮廓）")

    def register_baoyuan(self):
        """注册 .baoyuan 双击关联（写 HKCU，免管理员）。"""
        import winreg
        exe = sys.executable
        script = str(ROOT_DIR / "cnc_gui.py")
        cmd = f'"{exe}" "{script}" "%1"'
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\.baoyuan") as k:
                winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "baoyuan.cncfile")
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\baoyuan.cncfile") as k:
                winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "CNC 过程文件")
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                                  r"Software\Classes\baoyuan.cncfile\shell\open\command") as k:
                winreg.SetValueEx(k, None, 0, winreg.REG_SZ, cmd)
        except OSError as e:
            messagebox.showerror("关联失败", str(e))
            return
        messagebox.showinfo(
            "已关联", "双击 .baoyuan 文件将用本软件打开。\n"
            f"命令：{cmd}")

    def _refresh_views(self, invalidate: bool = True):
        """刷新左侧视图与画布。

        invalidate=True（默认，参数/顺序/几何改动）：失效活动板刀路缓存并
        清覆盖区，需重新点"生成刀路"；False 仅刷新显示（切板路径用，
        不丢已算刀路）。
        """
        if not self.job.result:
            return
        # 跨板：分组树聚合所有板（批量编辑）；列表/画布仍为活动板
        all_feats = [f for b in self.boards for f in b.result.features] or \
                    self.job.result.features
        board_of = {f.id: b.name for b in self.boards for f in b.result.features}
        closed_of = {c.id: c.closed
                     for b in self.boards for c in b.result.contours} or \
                    {c.id: c.closed for c in self.job.result.contours}
        self.params._closed_of = closed_of
        self.views._closed_of = closed_of   # 分组右键"矢量反向"判定用
        self.views.fill_features(self.job.result.features,
                                 self.job.result.contours,
                                 all_features=all_feats, board_of=board_of)
        # 参数可能变了：失效活动板缓存并清覆盖区数据（须在 set_data_keep
        # 之前——其内部 redraw 会画已清空的覆盖区）；切板路径不失效
        if invalidate and 0 <= self.active_idx < len(self.boards):
            self.boards[self.active_idx].moves_by_tool = {}
            self.boards[self.active_idx].feature_moves = {}
        self.view.coverage = None
        self.view.feature_coverage = {}
        self.view._cov_items = {}
        self.view.set_data_keep(self.job.result.features)
        # 刀路预览清空（参数可能变了）
        self.view.moves = []

    def _reverse_vector(self, fid: str):
        """开放链矢量反向：反转轮廓点序（画布箭头/刀路方向立即变向）。"""
        for b in self.boards:
            for c in b.result.contours:
                if c.id == next((f.contour_id for f in b.result.features
                                 if f.id == fid), None):
                    c.pts = list(reversed(c.pts))
                    self._refresh_views()
                    self.status.set(f"{fid} 矢量已反向")
                    return

    def export_cnc(self):
        if not self.job.result:
            messagebox.showinfo("提示", "请先打开 DXF")
            return
        # ---- 导出前检查 ----
        problems: list[str] = []
        lib = self.job.lib
        for f in self.job.result.features:
            if not f.params.enabled:
                continue
            if f.params.depth <= 0:
                problems.append(f"无深度：{f.label}")
            tool = lib.tools.get(f.params.tool_id)
            ct = next((c for c in self.job.result.contours
                       if c.id == f.contour_id), None)
            if tool and ct and ct.is_circle and ct.diameter:
                if tool.diameter > ct.diameter - 0.2:
                    problems.append(f"刀径过大：{f.label} 需要 ≤φ{ct.diameter - 0.2:.1f}")
        if problems:
            messagebox.showwarning("导出检查未通过", "\n".join(problems[:10]))
            return
        out = filedialog.asksaveasfilename(
            title="保存 CNC（每把刀一个文件）", defaultextension=".cnc",
            filetypes=[("宝元 CNC", "*.cnc")], initialfile="加工")
        if not out:
            return
        p = Path(out)
        written = self.job.export_by_tool(p.parent, p.stem)
        if not written:
            messagebox.showwarning("提示", "没有可导出的刀路（全部禁用或无深度）")
            return
        # 刀路预览（第一把刀，轨迹线维持现状）+ 覆盖区（全部刀参与，按各自刀径）
        moves_map = self.job.moves_by_tool()
        first = next(iter(moves_map.values()))
        self.view.set_toolpath(first)
        board = self.boards[self.active_idx] if 0 <= self.active_idx < len(self.boards) else None
        if board is not None:
            # 导出参数已定：现算存回板缓存，切回活动板时覆盖区/点选高亮与导出时一致
            board.moves_by_tool = moves_map
            board.feature_moves = self.job.moves_by_feature()
            self._update_coverage(board.moves_by_tool, board.feature_moves)
        # 同步保存过程文件（同名 .baoyuan，含几何快照 + 忽略层 + 刀路）
        bp = self._save_baoyuan(p.parent, p.stem, board=board, moves_map=moves_map)
        self.status.set("已导出：" + "  ".join(str(v) for v in written.values())
                        + f"  |  过程文件 {bp.name}")

    def export_batch(self):
        """批量导出：每板独立 CNC + 过程文件，板名作文件名。"""
        if not self.boards:
            messagebox.showinfo("提示", "请先批量导入 DXF")
            return
        out_dir = filedialog.askdirectory(title="批量导出到目录")
        if not out_dir:
            return
        if 0 <= self.active_idx < len(self.boards):
            self.boards[self.active_idx].ordered = self.job.ordered
        strategy = self._sort_names.get(self._sort_var.get(), "around")
        lib = self.job.lib
        # ---- 每板导出前检查 ----
        problems: list[str] = []
        for b in self.boards:
            if not b.ordered:
                self.job.result = b.result
                b.ordered = self.job.sort(strategy)
            for f in b.result.features:
                if not f.params.enabled:
                    continue
                if f.params.depth <= 0:
                    problems.append(f"[{b.name}] 无深度：{f.label}")
                tool = lib.tools.get(f.params.tool_id)
                ct = next((c for c in b.result.contours if c.id == f.contour_id), None)
                if tool and ct and ct.is_circle and ct.diameter:
                    if tool.diameter > ct.diameter - 0.2:
                        problems.append(f"[{b.name}] 刀径过大：{f.label}")
        if problems:
            messagebox.showwarning("批量导出检查未通过", "\n".join(problems[:10]))
            self._set_active(self.active_idx)
            return
        # ---- 导出 ----
        results: list[str] = []
        for b in self.boards:
            self.job.result = b.result
            self.job.ordered = b.ordered
            written = self.job.export_by_tool(out_dir, b.name)
            if written:
                self._save_baoyuan(Path(out_dir), b.name, dxf_path=b.path, board=b)
                results.append(f"{b.name}：" + "  ".join(str(v) for v in written.values()))
        self._set_active(self.active_idx)
        self.status.set("批量导出完成（" + str(len(results)) + " 板）")
        messagebox.showinfo("批量导出完成", "\n".join(results) or "无可导出内容")

    # ---------------------------------------------------------------- 联动
    def _features_by_ids(self, fids: set[str]) -> list:
        """跨板查特征（批量作业：分组树可选其他板的特征）。"""
        if self.boards:
            return [f for b in self.boards for f in b.result.features
                    if f.id in fids]
        return [f for f in (self.job.result.features if self.job.result else [])
                if f.id in fids]

    def _on_list_select(self, _e=None):
        fids = self.views.selected_feature_ids()
        self.view.select([i for i in fids if i in
                          {f.id for f in self.job.result.features}])
        self.params.show(self._features_by_ids(fids))

    def _on_canvas_select(self, _e=None):
        fids = self.view.selected()
        self.views.show_selection(fids)
        self.params.show(self._features_by_ids(fids))

    def open_tools(self):
        """打开刀具库管理窗口。"""
        ToolDialog(self, self.job.lib, CONFIG / "tools.json",
                   on_saved=self.params.refresh_tools)

    def open_rules(self):
        """打开规则集管理窗口（可视化编辑 rules.json）。"""
        RulesDialog(self, CONFIG / "rules.json",
                    tool_ids=list(self.job.lib.tools.keys()),
                    on_saved=self._apply_rules)

    def _apply_rules(self, rules: Rules):
        """规则集写入文件后，当前会话识别规则即时切换（重新导入 DXF 生效）。"""
        self.job.rules = rules


# ----------------------------------------------------------------------------
# 刀具库管理窗口
# ----------------------------------------------------------------------------

class ToolDialog(tk.Toplevel):
    """查看/新增/修改/删除刀具，保存到 config/tools.json。

    流程：左侧选中刀具 → 右侧表单回填 → 改参数 → [保存修改/新增] 更新内存；
    新刀具：清空表单（点 [清空表单]）→ 填写 → [保存修改/新增]；
    最后点 [写入文件] 持久化到 json（下次启动生效，当前会话即时生效）。
    """
    FIELDS = [
        ("id", "刀号（如 T3）"), ("number", "刀库位号"), ("diameter", "直径 mm"),
        ("type", "类型 endmill/drill/countersink"), ("spindle", "主轴转速 rpm"),
        ("plunge_f", "下刀进给 F"), ("feed_f", "切削进给 F"),
        ("max_stepdown", "最大切深 mm"), ("stepover_factor", "步距系数×刀径"),
        ("usage", "用途说明"),
    ]

    def __init__(self, master, lib: ToolLibrary, path: Path, on_saved=None):
        super().__init__(master)
        self.title("刀具库管理")
        self.geometry("880x430")
        self.lib = lib
        self.path = Path(path)
        self.on_saved = on_saved

        # 左：刀具列表
        cols = ("id", "dia", "type", "spindle", "feed", "usage")
        heads = {"id": "刀号", "dia": "直径", "type": "类型",
                 "spindle": "主轴", "feed": "切削F", "usage": "用途"}
        widths = {"id": 50, "dia": 50, "type": 80,
                  "spindle": 60, "feed": 60, "usage": 150}
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=15)
        for k in cols:
            self.tree.heading(k, text=heads[k])
            self.tree.column(k, width=widths[k], anchor=tk.W)
        self.tree.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 4), pady=8)
        # 三重保障：虚拟事件 + 鼠标点击（不依赖虚拟事件，直接按点击位置识别行）
        self.tree.bind("<<TreeviewSelected>>", self._on_select)
        self.tree.bind("<ButtonRelease-1>", self._on_click)

        # 右：编辑表单
        form = ttk.Frame(self)
        form.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 8), pady=8)
        ttk.Label(form, text="← 点击左侧刀具，此处显示全部参数（可编辑）",
                  foreground="#447").grid(row=0, column=0, columnspan=2,
                                          sticky=tk.W, pady=(0, 6))
        self.vars: dict[str, tk.StringVar] = {}
        for i, (key, label) in enumerate(self.FIELDS, start=1):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky=tk.W, pady=2)
            v = tk.StringVar()
            self.vars[key] = v
            ttk.Entry(form, textvariable=v, width=24).grid(
                row=i, column=1, pady=2, sticky=tk.EW)
        bar = ttk.Frame(form)
        bar.grid(row=len(self.FIELDS) + 1, column=0, columnspan=2,
                 pady=(12, 0), sticky=tk.W)
        ttk.Button(bar, text="保存修改/新增", command=self._apply).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="清空表单", command=self._clear).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="删除选中", command=self._delete).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="写入文件", command=self._save).pack(side=tk.LEFT, padx=2)
        form.columnconfigure(1, weight=1)
        self._fill()

    # ---------------------------------------------------------------- 操作
    def _fill(self):
        self.tree.delete(*self.tree.get_children(""))
        for t in self.lib.tools.values():
            self.tree.insert("", "end", iid=t.id, values=(
                t.id, f"φ{t.diameter:g}", t.type, f"{t.spindle:g}",
                f"{t.feed_f:g}", t.usage))

    def _on_click(self, ev):
        """鼠标点击兜底：按点击位置识别行并回填（不依赖虚拟事件）。"""
        row = self.tree.identify_row(ev.y)
        if row:
            self.tree.selection_set(row)
            self._fill_form(row)

    def _on_select(self, _e=None):
        sel = self.tree.selection()      # 不用 focus()：事件时 focus 可能滞后
        if sel:
            self._fill_form(sel[0])

    def _fill_form(self, tid: str):
        t = self.lib.tools.get(tid)
        if not t:
            return
        for key, _label in self.FIELDS:
            self.vars[key].set(str(getattr(t, key)))

    def _clear(self):
        for key, _label in self.FIELDS:
            self.vars[key].set("")
        self.vars["type"].set("endmill")
        self.vars["spindle"].set("18000")
        self.vars["plunge_f"].set("3000")
        self.vars["feed_f"].set("9000")
        self.vars["max_stepdown"].set("10")
        self.vars["stepover_factor"].set("0.9")

    def _apply(self):
        try:
            tid = self.vars["id"].get().strip()
            if not tid:
                raise ValueError("刀号不能为空")
            dia = float(self.vars["diameter"].get() or 0)
            if dia <= 0:
                raise ValueError("直径必须大于 0")
            tool = Tool(
                id=tid,
                number=int(self.vars["number"].get() or 0),
                diameter=dia,
                type=self.vars["type"].get().strip() or "endmill",
                usage=self.vars["usage"].get().strip(),
                spindle=float(self.vars["spindle"].get() or 18000),
                plunge_f=float(self.vars["plunge_f"].get() or 3000),
                feed_f=float(self.vars["feed_f"].get() or 9000),
                max_stepdown=float(self.vars["max_stepdown"].get() or 10),
                stepover_factor=float(self.vars["stepover_factor"].get() or 0.9))
        except ValueError as e:
            messagebox.showerror("刀具参数错误", str(e), parent=self)
            return
        self.lib.tools[tid] = tool
        self._fill()
        if self.on_saved:
            self.on_saved()

    def _delete(self):
        sel = self.tree.selection()
        tid = sel[0] if sel else ""
        if tid and tid in self.lib.tools:
            if messagebox.askyesno("删除刀具", f"确认删除 {tid}？", parent=self):
                del self.lib.tools[tid]
                self._fill()
                if self.on_saved:
                    self.on_saved()

    def _save(self):
        from dataclasses import asdict
        data = {"tools": [asdict(t) for t in self.lib.tools.values()]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        messagebox.showinfo("已保存", f"刀具库已写入\n{self.path}", parent=self)


# ----------------------------------------------------------------------------
# 规则集管理窗口
# ----------------------------------------------------------------------------

_RULE_KINDS = [("通孔", THROUGH_HOLE), ("沉孔", COUNTERBORE), ("钻孔", DRILL)]
_RULE_STRATS = [s for s in STRATEGIES if s[1] != VECTOR_CUT]


class RuleTab(ttk.Frame):
    """规则 Tab：左列表 + 右表单 + 底部 CRUD（mode: holes / layers）。"""

    def __init__(self, master, mode: str, tool_ids: list[str], write_cmd):
        super().__init__(master, padding=4)
        self.mode = mode
        self.data: list[dict] = []
        if mode == "holes":
            self.fields = [("diameter", "直径 mm", "entry"),
                           ("kind", "类型", "combo", _RULE_KINDS),
                           ("tool", "刀号", "combo_edit", tool_ids),
                           ("depth", "深度 mm", "entry"),
                           ("n_passes", "分刀次数（空=按步距）", "entry"),
                           ("stepdown", "步距 mm（0=按次数）", "entry"),
                           ("strategy", "策略", "combo", _RULE_STRATS),
                           ("stepover_factor", "行距系数(×刀径)", "entry"),
                           ("stepover_mm", "行距 mm（空=用系数）", "entry"),
                           ("parallel_axis", "平行方向", "combo", [("X", "X"), ("Y", "Y")]),
                           ("pocket_direction", "起刀方向", "combo",
                            [("从外到内", "outside_in"), ("从内到外", "inside_out")])]
            cols, heads, widths = (("c1", "c2", "c3", "c4", "c5", "c6"),
                                   ("直径", "类型", "刀", "深度", "分刀", "策略"),
                                   (45, 100, 40, 40, 40, 70))
        else:
            self.fields = [("name", "图层名（如 T01）", "entry"),
                           ("tool", "刀号", "combo_edit", tool_ids),
                           ("depth", "深度 mm", "entry"),
                           ("n_passes", "分刀次数（空=按步距）", "entry"),
                           ("strategy", "策略", "combo", _RULE_STRATS),
                           ("stepover_factor", "行距系数(×刀径)", "entry"),
                           ("stepover_mm", "行距 mm（空=用系数）", "entry"),
                           ("parallel_axis", "平行方向", "combo", [("X", "X"), ("Y", "Y")]),
                           ("pocket_direction", "起刀方向", "combo",
                            [("从外到内", "outside_in"), ("从内到外", "inside_out")])]
            cols, heads, widths = (("c1", "c2", "c3", "c4", "c5"),
                                   ("图层", "刀", "深度", "分刀", "策略"),
                                   (45, 40, 50, 40, 70))
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=14)
        for k, h, w in zip(cols, heads, widths):
            self.tree.heading(k, text=h)
            self.tree.column(k, width=w, anchor=tk.W)
        self.tree.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 4), pady=4)
        self.tree.bind("<<TreeviewSelected>>", self._on_select)
        self.tree.bind("<ButtonRelease-1>", self._on_click)
        form = ttk.Frame(self)
        form.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 8), pady=4)
        ttk.Label(form, text="← 点击左侧某行，此处显示全部参数（可编辑）",
                  foreground="#447").grid(row=0, column=0, columnspan=2,
                                          sticky=tk.W, pady=(0, 6))
        self.vars: dict[str, tk.StringVar] = {}
        self._rows: dict[str, tuple] = {}   # key → (label_widget, input_widget)
        for i, fd in enumerate(self.fields, start=1):
            key, label, kind = fd[0], fd[1], fd[2]
            label_w = ttk.Label(form, text=label)
            label_w.grid(row=i, column=0, sticky=tk.W, pady=2)
            v = tk.StringVar()
            self.vars[key] = v
            if kind == "entry":
                input_w = ttk.Entry(form, textvariable=v, width=24)
                input_w.grid(row=i, column=1, pady=2, sticky=tk.EW)
            else:
                input_w = ttk.Combobox(form, textvariable=v, width=22,
                                       state="readonly" if kind == "combo"
                                       else tk.NORMAL,
                                       values=[c[0] for c in fd[3]])
                input_w.grid(row=i, column=1, pady=2, sticky=tk.EW)
                if key == "strategy":      # 策略变化 → 动态显隐清槽子参数
                    input_w.bind("<<ComboboxSelected>>",
                                 lambda _e: self._update_strategy_rows())
            self._rows[key] = (label_w, input_w)
        bar = ttk.Frame(form)
        bar.grid(row=len(self.fields) + 1, column=0, columnspan=2,
                 pady=(12, 0), sticky=tk.W)
        ttk.Button(bar, text="保存修改/新增", command=self._apply).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="清空表单", command=self._clear).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="删除选中", command=self._delete).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="写入文件", command=write_cmd).pack(side=tk.LEFT, padx=2)
        form.columnconfigure(1, weight=1)
        self._update_strategy_rows()   # 初始按默认策略隐藏清槽子参数

    def fill(self, data: list[dict]):
        self.data = data
        self._refresh()

    def _refresh(self, keep: int = -1):
        self.tree.delete(*self.tree.get_children(""))
        kc = dict(_RULE_KINDS)
        sc = {c: n for n, c in _RULE_STRATS}
        for i, d in enumerate(self.data):
            if self.mode == "holes":
                vals = (f"φ{d.get('diameter', 0):g}",
                        kc.get(d.get("kind"), d.get("kind")), d.get("tool", ""),
                        f"{d.get('depth', 0):g}",
                        "" if d.get("n_passes") is None else d["n_passes"],
                        sc.get(d.get("strategy"), d.get("strategy")))
            else:
                vals = (d.get("name", ""), d.get("tool", ""),
                        f"{d.get('depth', 0):g}",
                        "" if d.get("n_passes") is None else d["n_passes"],
                        sc.get(d.get("strategy"), d.get("strategy")))
            self.tree.insert("", "end", iid=f"r{i}", values=vals)
        if 0 <= keep < len(self.data):
            self.tree.selection_set(f"r{keep}")
            self.tree.see(f"r{keep}")

    def _on_click(self, ev):
        row = self.tree.identify_row(ev.y)
        if row:
            self.tree.selection_set(row)
            self._fill_form(int(row[1:]))

    def _on_select(self, _e=None):
        sel = self.tree.selection()
        if sel:
            self._fill_form(int(sel[0][1:]))

    def _fill_form(self, idx: int):
        if not (0 <= idx < len(self.data)):
            return
        d = self.data[idx]
        for key, *_ in self.fields:
            v = d.get(key, "")
            self.vars[key].set("" if v is None else str(v))
        if self.mode == "holes":
            for cn, code in _RULE_KINDS:
                if code == d.get("kind"):
                    self.vars["kind"].set(cn)
                    break
        for cn, code in _RULE_STRATS:
            if code == d.get("strategy"):
                self.vars["strategy"].set(cn)
                break
        for cn, code in [("X", "X"), ("Y", "Y")]:
            if code == d.get("parallel_axis"):
                self.vars["parallel_axis"].set(cn)
                break
        for cn, code in [("从外到内", "outside_in"), ("从内到外", "inside_out")]:
            if code == d.get("pocket_direction"):
                self.vars["pocket_direction"].set(cn)
                break
        self._update_strategy_rows()   # 按选中行策略显隐清槽子参数

    def _clear(self):
        defaults = {"kind": "通孔", "strategy": "螺旋下刀",
                    "tool": "T1", "stepdown": "0",
                    "stepover_factor": "0.9", "stepover_mm": "",
                    "parallel_axis": "X", "pocket_direction": "从外到内"}
        for key, *_ in self.fields:
            self.vars[key].set(defaults.get(key, ""))
        self.tree.selection_remove(self.tree.selection())
        self._update_strategy_rows()   # 清空后策略回"螺旋下刀"，4 行隐藏

    # 仅偏置/平行清槽才显示的下一级参数字段
    _POCKET_ONLY = ("stepover_factor", "stepover_mm",
                    "parallel_axis", "pocket_direction")

    def _update_strategy_rows(self):
        """仅偏置/平行清槽显示行距/方向，其余策略隐藏（与主界面参数栏一致）。"""
        code = dict(_RULE_STRATS).get(self.vars["strategy"].get(), SPIRAL)
        for key in self._POCKET_ONLY:
            label_w, input_w = self._rows[key]
            if key in ("stepover_factor", "stepover_mm"):
                show = code in (OFFSET_POCKET, PARALLEL_POCKET)
            elif key == "parallel_axis":
                show = code == PARALLEL_POCKET
            elif key == "pocket_direction":
                show = code == OFFSET_POCKET
            else:
                show = False
            if show:
                label_w.grid()
                input_w.grid()
            else:
                label_w.grid_remove()
                input_w.grid_remove()

    def _collect(self) -> dict | None:
        d: dict = {}
        try:
            if self.mode == "holes":
                d["diameter"] = float(self.vars["diameter"].get() or 0)
                if d["diameter"] <= 0:
                    raise ValueError("直径必须大于 0")
                d["kind"] = dict(_RULE_KINDS).get(
                    self.vars["kind"].get(), THROUGH_HOLE)
                d["stepdown"] = float(self.vars["stepdown"].get() or 0)
            else:
                d["name"] = self.vars["name"].get().strip()
                if not d["name"]:
                    raise ValueError("图层名不能为空")
            d["tool"] = self.vars["tool"].get().strip() or "T1"
            d["depth"] = float(self.vars["depth"].get() or 0)
            if d["depth"] < 0:
                raise ValueError("深度不能为负")
            ns = self.vars["n_passes"].get().strip()
            d["n_passes"] = int(ns) if ns else None
            if d["n_passes"] is not None and d["n_passes"] < 1:
                raise ValueError("分刀次数需 ≥ 1")
            d["strategy"] = dict(_RULE_STRATS).get(
                self.vars["strategy"].get(), SPIRAL)
            d["stepover_factor"] = float(self.vars["stepover_factor"].get() or 0.9)
            if not (0 < d["stepover_factor"] <= 2):
                raise ValueError("行距系数需在 0~2 之间")
            sm = self.vars["stepover_mm"].get().strip()
            d["stepover_mm"] = float(sm) if sm else None
            if d["stepover_mm"] is not None and d["stepover_mm"] <= 0:
                raise ValueError("行距 mm 需大于 0")
            d["parallel_axis"] = dict([("X", "X"), ("Y", "Y")]).get(
                self.vars["parallel_axis"].get(), "X")
            d["pocket_direction"] = dict([("从外到内", "outside_in"),
                                          ("从内到外", "inside_out")]).get(
                self.vars["pocket_direction"].get(), "outside_in")
        except ValueError as e:
            messagebox.showerror("规则参数错误", str(e), parent=self)
            return None
        return d

    def _apply(self):
        d = self._collect()
        if d is None:
            return
        # 与刀具库同范式：以表单里的"键"判定新增/修改，左侧选中仅用于回填——
        # 孔规则键=直径（match_hole 按直径匹配，一个直径只留一条）；
        # 图层规则键=图层名。键已存在→覆盖该条（修改）；键不存在→追加（新增，原条目保留）。
        if self.mode == "holes":
            idx = next((i for i, x in enumerate(self.data)
                        if x.get("diameter") == d["diameter"]), -1)
        else:
            idx = next((i for i, x in enumerate(self.data)
                        if x.get("name") == d["name"]), -1)
        if idx < 0:
            self.data.append(d)
            idx = len(self.data) - 1
        else:
            self.data[idx] = d
        self._refresh(idx)
        self._fill_form(idx)

    def _delete(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0][1:])
        if not (0 <= idx < len(self.data)):
            return
        d = self.data[idx]
        name = d.get("name") or f"φ{d.get('diameter', 0):g} {d.get('kind', '')}"
        if messagebox.askyesno("删除规则", f"确认删除 {name}？", parent=self):
            del self.data[idx]
            self._refresh()


class RulesDialog(tk.Toplevel):
    """规则集管理：可视化编辑 rules.json（孔规则 + 图层规则）。

    交互与刀具库管理一致；[保存修改/新增] 只改内存，
    [写入文件] 才整体写回 rules.json 并让当前会话即时生效。
    """

    def __init__(self, master, path: Path, tool_ids: list[str], on_saved=None):
        super().__init__(master)
        self.title("规则集管理")
        self.geometry("725x430")
        self.path = Path(path)
        self.on_saved = on_saved
        data = self._load()
        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True)
        self.tab_holes = RuleTab(nb, "holes", tool_ids, self._write)
        nb.add(self.tab_holes, text=" 孔规则 ")
        self.tab_layers = RuleTab(nb, "layers", tool_ids, self._write)
        nb.add(self.tab_layers, text=" 图层规则 ")
        hd = asdict(HoleRule(diameter=0.0))
        self.tab_holes.fill(
            [{**hd, **h} for h in data.get("standard_holes", [])])
        ld = asdict(LayerDefault())
        self.tab_layers.fill([{"name": k, **ld, **v} for k, v in
                              data.get("layer_defaults", {}).items()])

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("rules.json 读取失败", str(e), parent=self)
            return {}

    def _write(self):
        holes = [{k: d.get(k) for k in ("diameter", "kind", "tool", "depth",
                                        "n_passes", "stepdown", "strategy",
                                        "stepover_factor", "stepover_mm",
                                        "parallel_axis", "pocket_direction")}
                 for d in self.tab_holes.data]
        layers = {d["name"]: {k: d.get(k) for k in ("depth", "n_passes",
                                                    "strategy", "tool",
                                                    "stepover_factor",
                                                    "stepover_mm",
                                                    "parallel_axis",
                                                    "pocket_direction")}
                  for d in self.tab_layers.data}
        data = {"standard_holes": holes, "layer_defaults": layers}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            messagebox.showerror("写入失败", str(e), parent=self)
            return
        if self.on_saved:
            rules = Rules()
            rules.standard_holes = [HoleRule(**h) for h in holes]
            rules.layer_defaults = {k: LayerDefault(**v)
                                    for k, v in layers.items()}
            self.on_saved(rules)
        messagebox.showinfo("已保存", "已保存到 rules.json", parent=self)


if __name__ == "__main__":
    app = App()
    # 窗口图标：取脚本同目录的 app.ico，文件缺失则静默忽略，保留默认图标
    try:
        app.iconbitmap(str(Path(__file__).with_name("app.ico")))
    except tk.TclError:
        pass
    if len(sys.argv) > 1 and sys.argv[1].lower().endswith(".baoyuan"):
        p = sys.argv[1]
        app.after(300, lambda: app._load_baoyuan(p))
    app.mainloop()
