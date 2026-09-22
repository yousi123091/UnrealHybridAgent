# 第二阶段总结：UnrealHybridAgent

**一句话**：在第一版「可信最小闭环」之上，把 UHA 强化成更适合 UE5 的 Hybrid Agent Runtime——语义优先判支撑、Router 多信号选路、GUI 会话标定、分层并发与可中止控制；**确定性的问题给规则，模糊的问题才给 AI**。

本文回答与第一版相同的三个问题：**交付了什么、验证到什么程度、还差什么。**  
原则未变：不推翻第一版架构；GitHub/官方已有且匹配的方案优先复用，但不为复用而复用。

---

## 1. 本次新增能力

| # | 能力 | 代码位置 | 一句话 |
|---|---|---|---|
| 1 | **SemanticSupportResolver** | `src/vision/semantic_support.py` | 先判断「该怎么被支撑」，再决定是否做悬空检查 |
| 2 | 语义优先巡检流水线 | `src/skills/visual_inspect.py` | 吊挂跳过告警；UNKNOWN/低置信 → Vision/AI |
| 3 | 精确落地（绝对坐标） | `src/vision/ground_place.py` + MCP `line_trace` | 采样射线 → 中位支撑面 → 绝对 Z → 幂等写入 |
| 4 | 幂等性默认收紧 | `src/skills/base.py` 及各 Skill | 未声明的写操作默认 **False**；只读/save 显式 True |
| 5 | Router 单出口多信号 | `src/router/router.py` / `cost.py` / `fallback.py` | 规则+成本+分类健康度+intent 信号，仍只输出一个 Decision |
| 6 | Router 选路质量账本 | `src/router/quality.py` | first-choice / fallback / regret，按 task_category |
| 7 | Session GUI Calibration | `src/desktop/calibration.py` + `uha calibrate` | 一次完整标定，多次布局校验复用 |
| 8 | Runtime Layout Validation | 同上 | 窗口/尺寸/DPI/显示器变了才重标 |
| 9 | Control Overlay | `src/desktop/overlay.py` | 任务/步骤/方式/是否控制键鼠；Pause/Stop/紧急停止 |
| 10 | Pause / Resume / Stop / EStop | `src/core/session_control.py` | 状态机；EStop 为确定性本地逻辑 |
| 11 | Emergency Hotkey | `src/desktop/hotkey.py` | 可配置（默认 Ctrl+Alt+Shift+F12），ctypes 注册 |
| 12 | 分层会话锁 | `src/core/session_locks.py` | 读并行 / 写串行 / GUI 写 = 写锁→桌面锁 |
| 13 | Executor 闭环接入 | `src/scheduler/executor.py` | 控制闸门、锁、质量记账、task_category 健康度 |
| 14 | GUI ROI/Ctrl+S 标定回退 | `src/skills/gui_helpers.py` | 配置缺失时用 Session Calibration，仍无则拒绝盲点 |

### 与第一版的衔接（刻意不改的）

* 执行方式最终裁决权仍在 **单一 Execution Router**（没有第二套并列 Planner 决策）。
* 三态验证、不可幂等重试护栏、`UNEXPECTED`/`NOT_SUPPORTED` 不进健康度、测试隔离生产状态——全部保留。
* 精确落地继续走 **绝对 location**，避免 F3 类「重试重复生效」。

---

## 2. 使用了哪些第三方项目/库

完整表见 [`docs/DEPENDENCIES.md`](DEPENDENCIES.md)。摘要：

| 项目 | 来源 / 版本 | License | 用来解决什么 | 为什么采用 | 可否替换 |
|---|---|---|---|---|---|
| **Agent-TARS Computer Use** | `<AGENT_TARS_ROOT>`（本地，HTTP :8788） | 见该仓库 | 桌面键鼠、截图、DesktopLock | 本机已在用；HTTP 共享锁，避免 stdio 多进程锁分裂 | 可换其它 CU MCP，需保持 lock 语义 |
| **GenOrca unreal-mcp + UnrealMCPython** | `<UNREAL_MCP_ROOT>`，v2.2.0 | Apache-2.0 | UE Actor/Level/Vision/**line_trace** | 与 UE 5.8 实测路径一致；UHA 只做 Adapter | 可换其它 UE MCP，需重做 DOMAIN_MAP |
| **pywin32** | 系统 `.venv` 已装 | PSF-2.0 | 找 UE 窗口、DPI、显示器 | Windows 原生、无重型 UI 框架 | 可 ctypes 裸调 user32，成本更高 |
| **httpx** | `.venv` | BSD-3-Clause | MCP Streamable HTTP | 成熟、依赖清晰 | 可用 urllib，能力缩水 |

### 3. 为什么采用它们 / 哪些最终自己实现

**采用判断**（每个大能力实现前检查）：

* 维护正常、License 兼容、与需求高度匹配、接入成本合理、**不明显增加系统复杂度**。
* 只解决约 20% 却带来大量依赖的方案 **不用**。
* 只需要成熟小库能解决的 **直接用**（如 pywin32、httpx）。
* 第三方一律 **Adapter/Wrapper**，不 fork 魔改。

**刻意未引入**：

| 候选 | 原因 |
|---|---|
| UI-TARS / 独立 GUI Grounding VLM | 确定问题走规则；调用方主 Agent 可看图。`vlmConfigured=false` 时绝不假装有语义定位 |
| 第二套 Planner/决策框架 | 执行裁决必须单出口，否则出问题无法归因 |
| `keyboard` / `pynput` 等热键库 | Emergency Stop 用 ctypes `RegisterHotKey` + 轮询回退即可，零新依赖 |
| 重型 UIAutomation / GUI Agent 框架 | Session 标定用「窗口几何 + UE 默认布局比例 + 可覆盖配置」已够用 |

**最终自己实现**（无现成库或复用后更复杂）：

* SemanticSupportResolver（UE 构件支撑语义，强领域）
* Router 多信号 + quality 账本（与 MethodHealth/Executor 深度耦合）
* Session 锁与并发策略（会话级产品策略）
* Control Overlay（极薄；tkinter 标准库，缺失时静默降级）
* ground_place 几何与绝对落点约定

---

## 4. SemanticSupportResolver 当前规则

输入：Actor Label / Name / Folder / Tags / Parent·Attachment / Mesh / Class /（可选）周边结构信息。  
输出：`support_mode` + `confidence` + `reason` + `evidence` + `recommended_check`。

| support_mode | 典型信号 | recommended_check | 含义 |
|---|---|---|---|
| **GROUND** | Plaza / Floor / Ground / 广场 / 地坪… | `ground_gap` | 相对**显式**地面做确定性几何检查 |
| **STRUCTURE** | Roof / Eave / Beam / Column / Wall / 台基… | `structure_trace` | 应被结构支撑，**不是**「必须落地」 |
| **ATTACHED** | Roof_Crown / wall ornament，且有结构上下文或 attachment | `attach_check` | 应贴附结构面；装饰≠可悬空 |
| **HANGING** | Hanging_Lantern / 吊灯 / pendant… | `none` | 语义上允许离地，跳过悬空告警 |
| **UNKNOWN** | 仅 “Decoration”、无信号、语义矛盾 | `vision` | 低置信 → Screenshot / 调用方 AI |

**重要约束**（写进测试）：

* **不会**把 Roof 简化成「必须落到地面」。
* **不会**因为名字里有 Decoration 就放行。
* Hanging decoration 走 HANGING，不误判成异常浮空。
* UNKNOWN / 低置信 / 语义与几何矛盾 → **才**进入 Vision/AI；不会用 AI 扫全场景每个 Actor。

高置信阈值：`HIGH_CONFIDENCE=0.75`。项目可用 config `semantic.rules` 覆盖命名规则。

巡检侧：`select_for_geometric_check()` 把结果分成 geometry / skipped_hanging / needs_ai；`visual_inspect` 在显式地面模式下会**过滤吊挂件**，避免「高于地面」被当成异常。

---

## 5. Router 当前评分方式

**仍然只有一个 Router。** 决策步骤：

```
探测可用性（含 UE TCP、冷却过滤）
  → rules.evaluate（规则候选 + bias）
  → 补全降级池（可用但未推荐的方法）
  → CostModel.estimate（每个候选）
  → 按 effective = score - bias 排序
  → 输出 Decision（首选 + fallback_order + considered 淘汰原因 + signals）
```

**多信号（内部，非第二套系统）**：

| 信号 | 来源 |
|---|---|
| A 规则 | `rules.py`（exact_values / known_actor / save_shortcut / hybrid_for_vision…） |
| B 成本/步骤 | `CostModel`：steps、latency、precision、risk、tool_cost |
| C/D 健康度 | `MethodHealth`，键为 **`task_category::METHOD`**（缺省回落全局 method） |
| E/F/G intent | needs_exact / needs_vision / is_batch / read_only |
| I 可用性 | MCP/Python/Commandlet/CU，含 MCP「进程在≠插件在」TCP 探测 |
| 操作风险 | cost 分量 `risk`（GUI 高于结构化） |

`UNEXPECTED`（本地代码）与 `NOT_SUPPORTED`（任务×通道不匹配）**不进**健康度（第一版 F3/F5 结论保留）。

---

## 6. 方法健康度如何分类

```
旧：METHOD                          全局成功率
新：task_category::METHOD           细分账本 + 兼容裸 METHOD 键
```

| task_category | 示例 |
|---|---|
| `actor_read` | actor_find / actor_inspect |
| `actor_mutation` | actor_move |
| `level_save` | level_save |
| `visual_inspection` | visual_inspect |
| `gui_interaction` | ui_only 任务 |
| `batch_mutation` | 批量修改 |

效果：`actor_read::HYBRID` 连续失败可冷却，**不会**把 `actor_mutation::HYBRID` 一起封杀。

---

## 7. GUI Calibration 工作方式

```
首次需要 GUI
  → find_ue_window（关键词含「Unreal Editor / 虚幻引擎 / UE5…」）
  → 取窗口与客户区、DPI、显示器
  → 按 UE 默认布局**相对比例**推 ROI（可用 gui.layout_overrides 覆盖）
  → 写 session cache（.state/gui_session.json）
之后每次 GUI 前
  → Layout Validation：hwnd / 位置 / 尺寸 / DPI / 显示器
  → 未变 → 复用缓存（不重截图、不重找控件）
  → 变了 → 自动完整重标定
```

**本机真机标定（2026-09-20）**：

* 窗口标题：`虚幻引擎5.8`，hwnd=`660150`
* 窗口：`x=208 y=93 w=1290 h=832`，monitor=`DISPLAY3`，dpi≈1.0
* `focus_point=[820, 450]`（viewport 中心，供 Ctrl+S 抢焦点）
* ROI：world_outliner / details_panel / viewport / toolbar / content_browser 已写入 cache
* 再次 `uha calibrate --validate`：**布局通过，可复用缓存**

CLI：

```bash
python uha.py calibrate            # 完整标定
python uha.py calibrate --force    # 强制重标
python uha.py calibrate --validate # 只做低成本布局验证
python uha.py roi-shot             # 人工手标用截图
```

---

## 8. Overlay 截图

本机当前 Python venv **未安装 tkinter**，Overlay 会静默降级（不影响主流程；控制状态机测试全部通过）。

设计如下（装上 tk 或使用带 Tcl/Tk 的 Python 后自动显示）：

* 置顶半透明窗，默认 `320x180+20+20`（可用 `desktop.overlay.geometry` 配置，避开 Outliner/Details/Viewport 核心区）。
* 实时显示：任务 / 步骤 / 执行方式 / 状态 / 下一步。
* **COMPUTER_CONTROL** 时红字：「Computer Use 正在控制鼠标和键盘，请暂时不要操作电脑。」
* 仅 ANALYZING / VERIFYING / STRUCTURED 时：「后台分析中，当前未控制鼠标。」
* 按钮：暂停 / 停止 / **紧急停止**（确定性钩子，不经过 LLM）。

逻辑状态见 `SessionController.snapshot().overlay_message`。

---

## 9–11. Pause / Stop / Emergency Stop 测试结果

| 概念 | 行为 | 自动化结果 |
|---|---|---|
| **PAUSE** | 停止**新的**写操作与键鼠；保留 Plan/进度；可 Resume | `phase=PAUSED`；`ensure_writable` 抛 `PreconditionFailed` |
| **RESUME** | 清除 PAUSE，恢复执行 | `wait_if_paused` 返回 True |
| **STOP** | 终止任务，停止 fallback，释放锁，不继续后续步骤 | `command=STOP`，`should_abort=True` |
| **EMERGENCY STOP** | 停队列 + **释放键鼠** + 释放 DesktopLock + 释放 UE 写锁 + 拒绝新 mutation + `ABORTED_BY_USER` | phase=`ABORTED`；钩子执行；再写被拒 |

**确定性**：EStop 不依赖 LLM / Vision / Router。钩子顺序：

1. `release_all_keys_and_buttons()`（修饰键/字母键 keyup + 鼠标三键 up）  
2. Computer Use `release_control(force=True)`（若适配器在线）  
3. `SessionCoordinator.force_release_all()`  
4. `controller.emergency_stop(reason=...)`

**热键**：`desktop.emergency_hotkey`，默认 `ctrl+alt+shift+f12`（与 UE 常用键冲突概率低），可配置；ctypes `RegisterHotKey`，失败则 `GetAsyncKeyState` 轮询。

测试：`tests/test_phase2.py::test_session_control` — **全部通过**。

---

## 12. 并发模型

```
READ / ANALYSIS          允许并行
  ├─ get_transform / find_actor / inspect
  ├─ screenshot / vision 分析
  ├─ SemanticSupportResolver
  ├─ Router cost / 规则评分 / 历史成功率
  └─ 几何计算 / 修复方案生成
           ↓ 汇总决策
UE_WRITE_LOCK            串行
  ├─ set_transform / spawn / save / rename
  ├─ blueprint / python mutation
  └─ 批量修改
           ↓（若需 GUI 改 UE）
DesktopLock              独占
  └─ mouse / keyboard / drag / hotkey
```

* 申请顺序固定：**UE_WRITE → Desktop**，避免死锁。  
* 只读路径不抢写锁；截图分析尽量不占桌面锁。  
* 有数据依赖/写冲突的任务仍串行——目标是缩短等待，不是堆线程。

---

## 13. 读并发是否真正提速

离线锁测试（`test_session_locks`）：

| 场景 | 指标 | 结果 |
|---|---|---|
| 8 个 READ 同时进入 | `max concurrent readers` | **8**（真正并行） |
| 5 个 WRITE | `max concurrent writers` | **1**（严格串行） |
| 4 个 ANALYSIS/READ | `max` | **4** |

说明读路径没有被全局大锁挡住。  
**端到端墙钟节省**尚未在真机 UE 上采样（MCP 插件未监听时无法跑满分析负载）；锁层数据已证明「读不互斥、写不重叠」。

---

## 14. 写锁实现

```python
# src/core/session_locks.py
class ReadWriteLock:          # 多读单写，写优先，防写饥饿
class SessionCoordinator:
    read()                    # 并行
    write()                   # UE_WRITE_LOCK
    desktop()                 # 独占桌面
    gui_mutation()            # write → desktop（固定顺序）
```

* Executor：mutation skill 或 `is_idempotent=False` 的步骤进 `write()`；MOUSE/KEYBOARD 进 `desktop()` 或 `gui_mutation()`。  
* 暂停/中止时 `ensure_writable` 直接拒绝。  
* 统计：`coordinator.stats()` — acquisitions / wait time / holders。

---

## 15–16. first-choice / fallback 与三个 Demo

### 选路质量账本

```bash
python uha.py quality      # first_choice_success_rate / fallback_rate / avg_attempts / regret
python uha.py stats        # 健康度（含 task_category::method）+ 质量摘要
```

当前本机：**质量账本为空**——真实结构化执行尚未在 MCP 插件在线时跑通；dry-run 只选路不记 outcome。

健康度快照（曾观测）：`UNREAL_MCP ok=22`（历史成功）+ TCP 不可达时的失败记录；`HYBRID ok=2`。

### Demo 结果（2026-09-20 实测）

| Demo | 目标 | 结果 |
|---|---|---|
| **A** 找 Actor → MCP → 绝对 Z → 验证 → 保存 | 结构化写闭环 | **阻塞**：UE 编辑器进程在，但 **UnrealMCPython TCP 12029 未监听**；`available()=False`（正确拒绝假绿） |
| **B** 疑似悬空 → SemanticResolver →（低置信则 Vision）→ line_trace 精确落地 → 复验 | 语义+精确修复 | **代码与离线几何已就绪**；真机待 MCP 插件开启后执行 `demo2 --precise` |
| **C** GUI：Session Calibration → Overlay → DesktopLock → CU → 释放 | GUI 可控性 | **标定真机成功**（见 §7）；Computer Use :8788 **OK**；Overlay 因无 tk 未弹出；Pause/EStop 逻辑测试通过；完整 GUI 操作链待 MCP/焦点联调 |

**Demo C 已验证到的真机事实**：

* 找到 UE 窗口并完成 Session ROI / focus_point 标定  
* 布局验证「可复用缓存」  
* Agent-TARS health：`ok=true, tools=17, lock 空闲, vlmConfigured=false`  

**要打通 A/B，请在 UE 编辑器中启用 UnrealMCPython 插件（127.0.0.1:12029）**，然后：

```bash
python uha.py doctor
python uha.py demo1 --actor Hall_Floor --delta 20
python uha.py demo2 --ground-ref EXT_Plaza_Central --region="-7500,-6500,-3700,-2700" --precise
python uha.py quality
```

---

## 17. 新增测试数量与通过情况

| 套件 | 数量 | 覆盖要点 | 结果 |
|---|---|---|---|
| `tests/test_offline.py` | **245** | 第一版契约/几何/三态/降级/幂等护栏/精确落地/生产状态隔离 | **全过** |
| `tests/test_router.py` | **24** | 路由场景、成本可解释性、可用性改变决策 | **全过** |
| `tests/test_phase2.py`（新增） | **85** | 语义分类、Roof/Decoration/Hanging、UNKNOWN→vision、幂等默认、task_category 冷却、Router quality、Pause/Stop/EStop、读并行写串行、标定复用/重标/DPI、Overlay headless、生产状态隔离 | **全过** |

**合计：354 项自动化断言通过。**  
所有测试使用临时 `stats/quality/gui_cache`，收尾断言确认 **生产 `routing_stats` 未被改动**。

---

## 18. 性能指标（先收真实数据，不为好看改逻辑）

| 指标 | 现状 |
|---|---|
| Router decision time | Executor 已记录 `performance.router_decision_ms`（日志/后续聚合） |
| first-choice success rate | 账本结构就绪；**待真机执行后有数** |
| fallback rate / avg attempts / regret | 同上（`uha quality`） |
| MCP / Vision / CU latency | 单步 duration 已在 jsonl；分类聚合待跑满 Demo |
| GUI recalibration count / cache hit | 标定 stats 已输出；本机 validate 证明可复用 |
| UE write lock wait / desktop lock wait | `coordinator.stats()` |
| parallel analysis time saved | 读并发已证明；墙钟差值待真机 |

---

## 19. 日志与可复盘

* 每次运行：`logs/runs/<name>.jsonl`（routing / fallback / health_skip / dry_run_stop / method_filter…）  
* 选路质量：`.state/router_quality.json`  
* 健康度：`.state/routing_stats.json`（含 `category::method`）  
* GUI 会话：`.state/gui_session.json`  
* 独立对账：`tools/observe.py`（不经 UHA 封装）  

---

## 20. GitHub 可发布性检查

| 约束 | 状态 |
|---|---|
| 不写死本机盘符绝对路径进仓库源码 | 第三方路径仅在 **gitignore 的本机** `config/agent.config.json`；example 可空 |
| 不写死分辨率/DPI/窗口 | 标定来自运行时探测 + session cache |
| 不写死 API Key / 工程 | 配置与 env（`UHA_*`）注入 |
| Overlay / Calibration / 锁可在别人电脑用 | 逻辑通用；依赖见 DEPENDENCIES |
| Emergency hotkey 可配置 | `desktop.emergency_hotkey` |

---

## 21. 已知问题

1. **UnrealMCPython 未监听 12029** → Demo A/B 无法在真编辑器里改场景（框架已正确报 DOWN，避免假绿）。  
2. **本机 venv 无 tkinter** → Overlay 不显示；状态机与按钮逻辑已测。  
3. **Session ROI 为启发式比例** → 首次真 GUI 操作前建议人工核对一次或 `layout_overrides` 微调。  
4. **Quality / 延迟聚合** 在 dry-run 不记 outcome；需真执行才有 first-choice 数据。  
5. **UE_PYTHON / Commandlet 活体** 仍不足（`bRemoteExecution` / 与已开会话互斥）。  
6. **无 Git 仓库**（PATH 亦无 git）→ 无法用 commit/diff 做版本说明。  
7. 语义规则对**非常规命名**可能 UNKNOWN——这是设计（交 AI），不是静默误判。

---

## 22. 下一阶段建议（优先级）

1. **打开 UE 插件 UnrealMCPython** → 跑 Demo A / Demo B `--precise`，用 `uha quality` 与 jsonl 回填 first-choice / fallback。  
2. **Demo C 完整链**：带 tk 的环境启动 Overlay → GUI 保存或 Outliner 查找 → 实测 Pause / Resume / Stop / 热键 EStop。  
3. **UE_PYTHON 活体**：编辑器开启 Remote Execution 后做契约测试，作为 MCP 故障时的正式合作通道。  
4. **按工程 profile 固化** `semantic.rules` 与 `gui.layout_overrides`。  
5. **用真实 runs 校准 CostModel** 先验（latency/success），只改权重不改「单 Router」架构。  
6. 健康度继续按 **skill_type × method** 积累样本，避免全局成功率再次误导选路（F5 教训）。

---

## 23. 结论：什么可信，什么不可信

**可以信**：

* 离线回归 **354** 项全过，且测试不污染生产账本。  
* 语义分类在规则可覆盖的命名上可复现；UNKNOWN 如实交给 AI 路径，不假装懂。  
* 精确落地路径设计为**绝对坐标 + 幂等**，与第一版 F3 护栏一致。  
* GUI：真机窗口标定成功，布局校验可复用；Computer Use 服务在线。  
* 框架在 MCP 插件离线时 **拒绝把通道标成可用**（假绿防护生效）。

**不要信**：

* 任何「接口 success=true」或「MCP 进程在 = 能改 UE」而不做回读/TCP 探活。  
* 未标定 ROI 上的盲点 GUI；启发式 ROI 未核对就当精确坐标。  
* 全场景无 ground_ref 的统计浮空结论（第一版 F1 结论未变）。  
* 尚无真机样本时的 first-choice 成功率数字（账本仍空）。

---

**最后一句（与第一版同构）**：账本和证据结构比「看起来跑通了」更重要；第二阶段把选路质量、语义分类、标定与控制都做成了**可测试、可复盘、可拒绝**的机制——真机 Demo 等插件上线后，用独立观测器对完账再写进质量账本。
