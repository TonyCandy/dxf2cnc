# DXF2CNC —— DXF 图纸自动识别生成 CNC 加工刀路

读取 DXF 图纸 → 自动识别 2.5D 平面加工特征（孔/沉孔/槽/轮廓/开放链）→ 图形界面设定刀具、深度、策略与走刀顺序 → 导出 CNC G 代码（宝元/LNC 控制器），并生成可复查的过程文件。

- 目标场景：平面多层板 2.5D 切割——钻孔、沉孔、凹槽、轮廓、偏置/平行清槽
- 全部在图形界面完成：识别、分组、参数、排序、预览、导出
- 后处理规则按目标控制器（宝元 LNC）实机样例反推固化

## 功能特性

**文件导入**
- 单文件/批量打开 DXF，每文件一块板；GBK/UTF-8 编码自动回退
- `.baoyuan` 过程文件：内嵌几何快照 + 全部参数 + 刀路快照，原 DXF 删除也可完整复现预览；支持系统双击关联

**自动识别**
- 规则识别 R1–R9：通孔/沉孔/钻孔/圆槽/矩形槽/一般口袋/开放链/外框/未知，规则表可配（`config/rules.json`）
- 同心孔配对（先通孔后沉孔）、外框自动标记不加工
- 忽略层过滤：尺寸线/中心线/文字等图层实体（DIMENSION/TEXT 展开显示，可勾选显隐）
- 图层规则精确匹配：直径提示仅认 `φ`/`𝛟`/`Φ`/`ｆ`/`⌀` 符号后接数字（如 `⌀11` → 11mm，纯数字图层名不解析）；槽默认参数按图层名精确匹配 `config/rules.json`（如 `T01` 层）
- 避让岛：图层名含「避让岛」的闭合环标记为岛，偏置/平行清槽自动避让口袋内部多个岛
- 手动纠错：特征可转移分组

**参数设置**
- 刀具 / 总深 / 分刀（次数或步深二选一）/ 策略 / 启用
- 策略：螺旋下刀、直下刀、偏置清槽（行距 + 起刀方向 外→内/内→外）、平行清槽（行距 + 方向 X/Y + 每层绕内壁精修）、轮廓（余量可调）、沿矢量加工（开放链，刀位左/右/居中）
- 应用到选中 / 应用到全组（批量跨板生效）
- 刀具库管理（写回 `config/tools.json`）、规则集管理（孔规则/图层规则双 Tab，写回 `config/rules.json`）

**排序**
- 站内固定先孔后槽；板间策略：绕板默认 / 按刀具分组 / 就近最少空跑 / 手动锁定
- 手动调整：双击序号键入目标值（其余自动顺延）、右键上移/下移/置首/置尾

**刀路生成**
- 螺旋孔、偏置清槽（圈间就近衔接、圈序可设）、平行清槽（含内壁精修环）、轮廓偏置、开放链双向走刀
- 多刀作业：按刀分组输出，一把刀一个文件

**预览**
- 滚轮缩放、中键平移、图层显示开关、外框/避让岛虚线
- 序号 + 直径合并标注、开放链方向箭头
- **刀轨覆盖区预览**：全刀刀径扫掠并集（深绿填充），点选特征对应覆盖区高亮，漏切区域（未扫到洞）橙红显示——直观判断清料是否干净
- 忽略层内容显示（尺寸标注等）
- 列表 / 分组 / 画布 / 参数四向联动选择

**导出**
- 宝元 `.cnc`：N3 起行号、模态轴省略、4 位小数坐标、CRLF、无圆弧指令（折线离散）
- 导出同时生成同名 `.baoyuan` 过程文件；批量导出逐板检查（无深度/刀径过大拦截）

## 技术栈

- Python 3.13（Windows，实际开发环境）
- GUI：tkinter / ttk（标准库）
- 运行时依赖仅 2 个：`ezdxf`（DXF 读取）、`shapely`（多边形偏置/布尔）
- 测试：pytest

## 架构与数据流

```
DXF → io_dxf（DxfData：entities/ignored + layer_roles）
    → recognize（RecognitionResult.features/contours，用 rules.json）
    → sort.sort_features（绕板/按刀/就近/手动）→ ordered
    → toolpath.gen_feature_moves（逐特征）→ moves（FEED/RAPID/PLUNGE/RETRACT）
    → cam.export_by_tool → post_baoyuan 写 .cnc；GUI 写 .baoyuan（含刀路快照）
打开 .baoyuan → 恢复几何 + 参数 + 刀路快照 → 画布预览
```

**关键数据结构**（model.py）：

- `FeatureParams`：tool_id / depth / stepdown / n_passes / strategy / climb /
  stepover_factor（行距系数）/ stepover_mm / parallel_axis（平行清槽方向 X/Y）/
  pocket_direction（偏置清槽 外→内/内→外）/ vector_side（矢量刀位 左/右/居中）/
  contour_side（轮廓刀位 自动/内/外）/ allowance（余量）/ enabled
- `Move`：kind（FEED/RAPID/PLUNGE/RETRACT）/ x / y / z / f；x/y 为 None 表示纯 Z 段
- `Board`（板级缓存）：几何/参数/排序/`moves_by_tool`/`feature_moves`，
  切板只读复用，"生成刀路"按钮与导出时刷新

模块依赖方向（单向，勿反依赖）：

```
io_dxf → geom → recognize → model ← toolpath → post_baoyuan
                            ↑            ↑
                        sort.py      cam.py（管线，测试与 GUI 共用）
                                    cnc_gui → panels / canvas_view
```

## 目录结构

```
├── cnc_gui.py          # 入口：主窗、工具栏、导入/导出、.baoyuan 存取、规则集/刀具库对话框
├── cam.py              # 管线：load → recognize → sort → moves_by_tool → export_by_tool
├── io_dxf.py           # DXF 读取：编码回退、实体离散、图层角色分类、忽略层实体展开
├── geom.py             # 拼环（图层隔离）、形心/包含关系、弦高离散、shapely 偏置
├── recognize.py        # R1–R9 识别规则、同心配对、外框标记、分组；规则集数据结构
├── model.py            # 数据结构（Entity/Contour/Feature/Tool/JobConfig/Move/FeatureParams）
├── toolpath.py         # 刀轨生成：螺旋/直下/偏置/平行+内壁精修/轮廓/沿矢量；覆盖区计算
├── sort.py             # 站内优先级 + 绕板/按刀/就近/手动排序
├── post_baoyuan.py     # 后处理：Move[] → CNC 文本
├── canvas_view.py      # 画布：缩放平移、标注排布、方向箭头、刀路与覆盖区预览、忽略层
├── panels.py           # 左侧三标签 + 参数面板 + 右键菜单
├── tests/              # pytest 回归（识别/排序/刀轨/后处理/黄金对比）
├── config/             # tools.json 刀具库、rules.json 识别规则
├── 启动.bat            # 双击启动（python cnc_gui.py）
└── 启动隐藏.vbs        # 无窗口启动
```

> 注：样例图纸与黄金 CNC 属业务数据，不随仓库分发；`tests/` 中的黄金对比测试需在本地 `基础数据文件/` 目录准备样例后运行。

## 安装与运行

```powershell
pip install ezdxf shapely
# 跑测试需要
pip install pytest
```

```powershell
# 方式一：双击 启动.bat
# 方式二：
python cnc_gui.py
```

导出：「生成 CNC」（单板）或「批量导出」（多板选目录），输出 `板名-T刀号.cnc`，同时生成同名 `.baoyuan`。

## 配置说明

- `config/tools.json`：刀具库（id/number/diameter/type/spindle/plunge_f/feed_f/max_stepdown/stepover_factor/usage），GUI「刀具库」可视化编辑
- `config/rules.json`：标准孔径表、图层忽略/角色规则（忽略层/钻孔层/刀号层/剖面层；直径图层名支持 `φ`/`𝛟`/`Φ`/`ｆ`/`⌀`）、图层默认深度（按图层名精确匹配），GUI「规则集」可视化编辑

## 已知限制

- 仅支持 CIRCLE / LINE / ARC / LWPOLYLINE 实体；HATCH/DIMENSION/TEXT 默认忽略（可配置显示）
- 无 G02/G03：圆弧一律弦高离散为 G1 折线
- 单刀单文件：多刀作业导出多个 `.cnc`，文件内无中途换刀
- 端点间隙 >0.05mm 的线段不自动闭合（保留为开放链）
- 刀心轨迹线预览当前仅显示第一把刀（覆盖区预览为全部刀具）
- 同一 DXF 导出的 `.baoyuan` 请勿与其他文件混选导入（特征 id 不加板名前缀会冲突）；单选正常

## 开发注意事项

- L4 中性刀路（`Move[]`）不得出现行号/G90/M30 等输出层概念，那是 `post_baoyuan.py` 的职责
- 改刀轨或后处理后建议跑回归：`python -m pytest tests/ -v`
- 硬约束：DXF 读取保留 GBK 回退；输出 CRLF + ASCII、坐标 4 位小数、F 1 位小数、行号 N3 起连续、字地址间无空格、模态轴省略
- GUI 事件绑定：`<<TreeviewSelect>>` 虚拟事件在部分环境点击不触发，树选择绑 `ButtonRelease-1` 兜底
- 项目为独立实现，未参考 GPL 项目源码

**刀路缓存与失效**：参数/顺序改动清活动板 `moves_by_tool`/`feature_moves` 与画布刀路；切板不清缓存；刀路只来自"生成刀路"按钮 / 导出 / `.baoyuan` 快照，无懒加载现算。批量导出循环里逐板切 `job.result`，存 `.baoyuan` 必须显式传 board，禁止读 `job.data`（不是当前板）。

**tkinter 无透明**：半透明效果用 `stipple`；外环+洞平铺进同一个 `create_polygon` 会让填充规则失效、洞被填——外环和洞必须分开画。

**覆盖区语义**：绿 = 全部策略 FEED 段按各自刀径 buffer 的并集（含轮廓/螺旋的刀径宽环带）；红 = 仅清槽"该清未清"区 = 目标可达区（口袋内缩 reach 再外扩 D/2，`reach = D/2 - (pocket_overcut - allowance)`）− 总覆盖。螺旋/轮廓环心、避让岛是洞不是漏切，只挖黑不报红。

**覆盖区绘制结构**（重构后，勿回退）：①覆盖区整层先于几何创建（z 序在几何之下），洞块/条带永不遮挡几何；②条带 = 单外环 stipple（gray75）；③洞区递归绘制：`bg = hole − coverage` 逐 part 画背景色垫底 + `covered = hole ∩ coverage` 逐 part 补条带色 + covered 的 interiors 递归（深度=同心层数）。勿改回"vis 只画 exterior"（interiors 丢失会多盖相邻条带），也勿把洞块垫在条带之下（单外环填充会把洞区误填绿）——两者均已实测证伪。

## 开发工作流

- 一个功能一份编号任务文档（`00X.md`），只描述该任务涉及的文件与验收标准，改完即实测验收；不顺手改任务外文件
- 覆盖区/刀路相关任务验收固定含：`python -m pytest tests/` 回归 + 真实 GUI 实机截图
- 增量备份：`git add` 具体文件 → `git commit` → `git push`（远程 `main`）

## 开发历程

| 阶段 | 内容 |
|---|---|
| 初始版本 | DXF 读取、R1–R9 识别、排序、六种策略刀路、宝元后处理、GUI 主框架 |
| 000 | 刀轨覆盖区预览（全刀并集 / 点选高亮 / `.baoyuan` 存刀路快照） |
| 001 | "生成刀路"按钮（弃懒加载，只算当前板） |
| 002 | 覆盖区洞填充修复（外环/洞分开画） |
| 003 | 规则集补清槽下一级参数 + 动态显隐（孔规则/图层规则双 Tab） |
| 005 | PyInstaller 打包（onedir + 应用图标），tag `v0.1.0` |
| 007 | 覆盖区不再遮挡点选特征 |
| 015 | 覆盖区语义修正：轮廓/螺旋刀径条带纳入显示、环心不红、漏切红仅清槽显式计算、避让岛不红；轮廓新增刀位参数（自动/内/外） |
| 016 | 覆盖区绘制重构：洞区递归绘制 + 覆盖层下沉，修复勾选覆盖区后内部图形消失/环形条带不显示 |
