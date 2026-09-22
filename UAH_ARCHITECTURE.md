> Historical documentation: UAH is disabled in this publication snapshot and deferred to the next release.

> Publication copy: personal user/evidence paths are redacted. Historical test results are unchanged; raw evidence remains private.

# UAH 架构判断（Universal Agent HUD）

> 本文是**审计结论 + 架构决策**，写在动手之前（后续按实测结果回填了修正）。
> 所有判断来自对 `E:/UnrealHybridAgent` 的实际代码阅读与实机执行，不是假设。
> 审计时间：2026-09-21。基线提交：`2f45122 fix(p0): computer use emergency stop, safety banner,
> input gate, fail-closed CU`。

---

## 0. 审计摘要（先给结论）

| 项目 | 实际情况 |
|---|---|
| 主项目位置 | `E:/UnrealHybridAgent`（分支 `p0-cu-safety`，HEAD `2f45122`） |
| 语言 | Python 3.13（`.venv` = 3.13.14，`home` 指向托管解释器） |
| 现存 UI **①** | `src/desktop/overlay.py::ControlOverlay`（tkinter 任务浮窗，6 行 Label + 3 个按钮） |
| 现存 UI **②** | `src/safety/banner.py::SafetyBanner`（P0 安全横幅，置顶、不抢焦点、只管 Computer Use 安全态） |
| **UI 是否真的在跑** | **在本机 `.venv` 下都起不来** —— `.venv` 里**没有 tkinter**；两个 UI 都是 `ImportError → 静默降级`（`overlay.py:11-14`、`banner.py:11-14`），`available=False` |
| 状态数据来源（运行时） | `src/core/session_control.py::SessionController`（进程内单例，`get_controller()`） |
| 状态数据来源（安全） | `src/safety/controller.py::SafetyController`（**另一套**、职责不同的状态：Computer Use 是否可动键鼠） |
| 已有观察机制 | ✅ `SessionController.on_change(fn)` 观察者钩子**已经存在** |
| 已有 IPC | ❌ 无 websocket / HTTP / socket / named pipe。日志是 JSONL **文件** |
| UI 与 Core 耦合度 | **低**。UI 只读 `controller.snapshot()`，不 import 执行逻辑 |
| 测试框架 | 无 pytest。测试是**可执行脚本**（`python tests/test_xxx.py`），自打印"全部通过（N 项）" |
| 构建方式 | 无构建。`python uha.py <cmd>` 直接跑 |
| 现有依赖 | `.venv` 152 个包（多为 MCP/HTTP 相关），**但 UAH 不需要任何一个** |
| 回归基线 | 388 项全绿：offline 248 / router 25 / phase2 85 / phase3 11 / phase4 13 / phase4b 6 |

**审计中最重要的两条发现**：

1. **UHA 当前的两份 UI 在这台机器的 `.venv` 下都起不来**（缺 tkinter）。
   这把"UAH 内嵌 UI 与独立 HUD 版本漂移"的风险直接降到零 —— 因为现在只有
   **一份**真正能被看见的任务状态展示（也就是没有），而且两个 UI 在结构上都
   已经被隔离成"只读快照的订阅者"。见 §5 与 §8。

2. **审计期间发现 HEAD 提交本身有一个语法错误**：
   `src/scheduler/executor.py:515` 有一整块 P0 安全代码被**多缩进了 4 格**
   （`IndentationError: unexpected indent`），导致 `import src.scheduler` 直接失败、
   **整个 CLI 无法运行**（`uha.py demo1 ...` → 崩在 import 阶段）。
   这是**先于本任务存在**的问题，不是 UAH 引入的。为让"真实运行测试"这件事成立，
   做了**最小修复：只把那一段的缩进改回去，不改任何语义**（见 `UAH_PHASE1_REPORT.md`
   的 Files Changed）。如果你正在并行编辑这一处，请以你的版本为准。

---

## 1. 当前 UHA UI 架构

装配点只有一个：`uha.py::_setup()`。它按顺序做四件事 —— 建日志、装配后端、
建 Executor、挂"展示与控制"层。展示与控制层现在有**两块**：

```
uha.py::_setup()
   │
   ├─ log = RunLogger(...)                       ← 人类日志 + 结构化 JSONL
   ├─ bundle = build_bundle(cfg)                  ← 后端集合（MCP / UE Python / Commandlet / Computer Use）
   ├─ executor = Executor(bundle, cfg, log)       ← 执行闭环
   │     └─ self.controller = get_controller()    ← ★ 进程内单例（任务状态真源）
   │
   ├─ safety = SafetyController / SafetyBanner / EmergencyHotkey   ← P0 安全层
   │     └─ 有自己的 SafetyState（IDLE/REQUESTED/ACTIVE/RELEASED/EMERGENCY_STOP）
   │        与 safety 自己的快照，**与任务状态是两件事**
   │
   └─ overlay = ControlOverlay(controller)        ← ★ 任务状态浮窗
```

### UI ①：`ControlOverlay`（任务状态）

`src/desktop/overlay.py`，160 行：

- 一个 **daemon 线程** 里跑 `tk.Tk()` + `root.mainloop()`；
- `root.attributes("-topmost", True)`，默认几何 `320x180+20+20`；
- `root.after(poll_ms=200, _tick)` —— **200ms 轮询** `controller.snapshot()`；
- 6 个 Label（title/task/step/method/phase/status/next）+ 1 个 warn Label + 3 个按钮（暂停/停止/紧急停止）；
- 显示条件：`st.should_show_overlay or self._visible`，即 IDLE / DONE 时自动 `withdraw()`；
- 无 tkinter 时：`self.available = False`，`start()` 直接 return，**不报错**。

### UI ②：`SafetyBanner`（Computer Use 安全）

`src/safety/banner.py`，P0 引入。它**刻意独立于任务 HUD**：`"Independent of agent HUD.
Does not steal focus."`，只反映 Computer Use 安全态（ACTIVE / WAITING / EMERGENCY / FINISHED），
并常驻显示 `Ctrl+Alt+F12 = EMERGENCY STOP`。

> 结论：两块 UI 的**职责是分开的**，这一点 UHA 做对了。
> `SafetyState` 与 UAH 的 `Status` **不是重复定义**：一个说"键鼠现在归谁"，
> 一个说"Agent 正在做什么"。Phase 1 里 UAH **完全不碰** SafetyBanner，
> 也不试图把它并进协议（并进去会把两套语义搅在一起）。
>
> `ControlOverlay` 的可取之处是**耦合极低**（只依赖 `SessionController` 的两个方法）；
> 它的不足正好是 UAH 要补的：无协议、无事件驱动、无跨进程、无多 Agent、无提醒。

---

## 2. 当前状态数据来源（唯一真源）

**`SessionController`（`src/core/session_control.py`）就是 UHA 的状态单一真源。**

```python
@dataclass
class ControlState:
    phase: TaskPhase = TaskPhase.IDLE        # IDLE/ANALYZING/STRUCTURED_EXECUTION/
                                             # COMPUTER_CONTROL/VERIFYING/PAUSED/
                                             # STOPPING/ABORTED/DONE/FAILED
    task: str = ""                           # 当前 skill 名（executor 传 skill.name）
    step: str = ""                           # 当前步骤（同上，步级语义较弱）
    method: str = ""                         # UNREAL_MCP / UE_PYTHON / MOUSE / HYBRID ...
    status_text: str = ""                    # "路由中" / "执行中"
    next_step: str = ""
    controls_mouse_keyboard: bool = False     # ★ 是否正在占用键鼠
    command: ControlCommand = NONE            # NONE/PAUSE/RESUME/STOP/EMERGENCY_STOP
    abort_reason: str | None = None
    updated_at: float
```

写入方（**只有 Executor 写**，UI 只读）：

| 位置 | 调用 |
|---|---|
| `executor.py:228` | `begin_task(skill.name)` |
| `executor.py:229` | `set_step(skill.name, status="路由中")` |
| `executor.py:359/376/413` | `set_method(method)` |
| `executor.py:414/516` | `set_step(skill.name, method=..., status="执行中")` |
| `executor.py:530/533/537/540` | `set_phase(COMPUTER_CONTROL / STRUCTURED_EXECUTION / ANALYZING)` |
| `executor.py:586` | `set_phase(VERIFYING)` |
| `executor.py:260/382/390/425/442` | `end_task(ok=True/False)` |

通知机制：`_update()` → `_notify()` → 遍历 `self._listeners` 逐个调用。
**`on_change(fn)` 已经是一个现成的、事件驱动的观察者入口。**

第二类状态源（**结构化、逐步骤、可复盘**）：`RunLogger` → `logs/runs/<run-id>.jsonl`。
它不是"实时状态"，是**事后证据**。有多条 `kind`：`routing` / `step` / `fallback` / `plan_start` /
`method_preference` / `dry_run_stop` / `method_filter` / `health_skip`。

第三类：`.state/checkpoints/task-*.json` —— 任务级快照与回滚记录，**不是实时状态**。

---

## 3. UI 与 Agent Core 的耦合点

**只有一处**：

```
ControlOverlay ──读──> SessionController.snapshot()   （+ on_pause/on_stop/on_emergency 三个回调）
```

具体依赖清单（`overlay.py` 的全部 import）：

```python
from ..core.session_control import SessionController, TaskPhase   # 仅此一项
```

- `ControlOverlay` **不** import `scheduler` / `router` / `skills` / `adapters`；
- **不** import 任何执行逻辑；
- **不**直接读日志、**不**碰文件；
- 控制方向（pause/stop/estop）走的是 `SessionController` 已经暴露的方法。

另一处弱耦合：`uha.py:88-89` 把 overlay/hotkey 挂到 executor 上（`executor._overlay = overlay`），
**但没有任何生产代码读这两个属性** —— 只有 `_run_demo()` 收尾时 `getattr(executor, "_overlay")` 用来 `stop()`。
即：**UI 不是 Agent 主逻辑的一部分**，这一点 UHA 已经做对了，UAH 直接沿用。

> 结论：耦合点收敛在 `SessionController` 这一个类上。
> **UAH 的接入点就是它，不需要动 Executor、Router、Skills、Adapters 的任何一行。**

---

## 4. 哪些代码可以直接复用

| 资产 | 复用方式 |
|---|---|
| `SessionController.on_change()` | **直接作为 UAH 的事件源**（事件驱动，零轮询） |
| `SessionController.snapshot()` | 断线重连、初次同步时的**全量状态**来源 |
| `ControlState` 全部字段 | 映射为 UAH `task/stage/step/activity/tool` 的输入 |
| `TaskPhase` 枚举 | **直接映射**为 UAH `Status`，**不新造 enum** |
| `ControlCommand` | 用于判定 PAUSED / CANCELLED 的方向 |
| `ApprovalGate._confirmer` 钩子 | **包裹**它即可发出 `WAITING_APPROVAL`（`uha.py:55` 已经用同样手法改写它） |
| `overlay.py` 的"无 tkinter 静默降级"模式 | HUD 沿用同样的降级策略 |
| `RunLogger.event(kind, **fields)` | 作为 UAH 的**离线审计副本**写入点 |
| 现有测试脚本风格（自打印 N 项） | UAH 测试沿用同一风格，**不引入 pytest** |

---

## 5. 哪些代码应该抽象

| 现状 | 抽象为 | 理由 |
|---|---|---|
| `TaskPhase`（10 个执行阶段） | UAH `Status`（9 个 Presence 状态） | 执行阶段 ≠ 存在状态。`ANALYZING / STRUCTURED_EXECUTION / COMPUTER_CONTROL / VERIFYING` **全部**是 `RUNNING`；不抽象就会把"实现细节"泄进协议 |
| `overlay.py` 里的 Label 布局与颜色 | `uah/ui/components/`（纯函数 + 共享控件工厂） | 两套宿主（内嵌/桌面）必须画同一张卡 |
| `should_show_overlay` 判定 | `uah/core/state.py` 的可见性/过期判定 | 同一语义要在协议层定义一次 |
| `.state/*.json` 的零散持久化 | `uah/core/protocol.py` 的单一序列化入口 | 状态字段必须一处定义 |
| `desktop/overlay.py` 的 200ms 轮询 | UAH 事件流（SSE） | 需求 §十八 明确禁止高频轮询 |

---

## 6. 哪些代码不能移动

**Phase 1 一律不动**（需求 §十五：不阻塞 UHA 当前开发）：

- `src/scheduler/executor.py`（40k，最核心的执行闭环） —— **一行不改**
- `src/router/**`、`src/skills/**`、`src/adapters/**`、`src/vision/**`、`src/validation/**` —— 不动
- `src/core/session_control.py` 的**语义** —— 不改状态机、不改 `begin_task`/`end_task` 的
  "不得清掉 PAUSE/STOP/ESTOP" 规则（那是踩过坑的护栏）
- `src/desktop/overlay.py` —— **保留**，不删不重写（需求 §十四）
- `tests/**` —— 不动（需求 §二十二）
- `src/safety/**`（`controller.py` / `banner.py` / `watchdog.py`）—— **P0 安全层，一行不动**。
  UAH 是 presence 层，绝不参与"键鼠归谁"的判断
- `P0_COMPUTER_USE_SAFETY_REPORT.md`、`PHASE4B_REPORT.md` —— 别人的交付文档，不动
- `config/agent.config.json` —— 只**追加** `uah` 段，不改动已有键值
- 依赖版本 —— 不新增任何第三方依赖，也就不可能改动版本

**唯一的一处例外（已在报告里单列）**：`src/scheduler/executor.py:515` 的缩进语法错误
是先于本任务存在的，且让整个 CLI 无法启动。为了"真实运行测试"这一条硬要求，
只把那段被多缩进的代码**改回正确缩进**（语义零变化）。除此之外 executor.py 一行未动。

---

## 7. 推荐的 UAH 目录结构

**决策：UAH 放在 UHA 仓库内，作为子目录 `uah/`，而不是另起一个仓库。**

理由（与需求 §一 一致）：
1. 需求要的是"一个状态协议 + 一个状态核心 + 多宿主"，**不是**两个独立项目；
2. 同一仓库 = 同一个 Git 历史 = `uah-core` 与 UHA 适配器**不可能版本漂移**；
3. `import uah` 零额外配置，不需要打包/发版/私有源；
4. 桌面 HUD 仍可**独立进程**运行（脚本独立、只有代码共享）—— 这就满足"解耦"了。

实际落地结构（相对需求 §十六 建议版的**两处调整**，见 §10.2）：

```
E:/UnrealHybridAgent/
├─ uha.py                      ← 只追加一个 try/except 块（~6 行）
├─ uah/                        ← ★ 新子项目
│  ├─ __init__.py
│  ├─ core/
│  │  ├─ protocol.py           # PROTOCOL_VERSION / 事件类型 / 兼容性判定
│  │  ├─ models.py             # ★ Status / AgentRef / TaskState / Activity / Snapshot / Event
│  │  ├─ state.py              # ★ StateStore（唯一状态机，事件→快照）
│  │  ├─ events.py             # EventBus（进程内 pub/sub）
│  │  └─ transport.py          # 本机 HTTP + SSE：HubServer / HubClient
│  ├─ adapters/
│  │  ├─ uha/adapter.py        # UHA Native Adapter（SessionController → UAH 事件）
│  │  └─ generic/bridge.py     # Generic Adapter（任意 JSON → UAH 事件）
│  ├─ ui/
│  │  ├─ components/card.py    # 卡片渲染（纯函数，两宿主共用）
│  │  ├─ components/tk_card.py # tkinter 控件工厂（两宿主共用）
│  │  └─ notify.py             # 提醒策略（去重 / 静音 / 多 sink）
│  ├─ hosts/
│  │  ├─ embedded/host.py      # 内嵌宿主（UHA 进程内）
│  │  ├─ embedded/bootstrap.py # maybe_start_embedded_uah(cfg, log, executor)
│  │  ├─ desktop/hud.py        # ★ 独立桌面 HUD（置顶小窗）
│  │  ├─ desktop/text_hud.py   # 无 tkinter 的纯文本 HUD（本机必需，见 §10.2）
│  │  └─ desktop/launch.py     # 自动挑一个带 tkinter 的解释器
│  ├─ tools/uah.py             # 独立 CLI：hub / emit / watch / notify / selftest
│  ├─ tests/test_uah_phase1.py # Test 1-10，自打印 N 项
│  └─ docs/                    # 协议说明等补充文档
├─ UAH_ARCHITECTURE.md         # 本文
└─ UAH_PHASE1_REPORT.md        # 交付报告
```

---

## 8. UHA → UAH 的迁移方式（**零改写**）

```
① UHA Agent Core（一字不改）
   src/scheduler/executor.py  ──写──▶  SessionController
   src/core/session_control.py
                                          │
② UAH 适配器（新增，只读订阅）             │ on_change(fn)   ← 现成钩子
   uah/adapters/uha/adapter.py  ◀─────────┘
        │  ControlState → UahEvent
        │  TaskPhase   → Status（唯一映射表）
        ▼
③ UAH Core（新增）
   uah/core/state.py::StateStore（唯一状态机）
        │
        ├──▶ uah/core/transport.py::HubServer（127.0.0.1 HTTP + SSE）
        │         ▲                        │
        │         │ POST /event            │ GET /state  GET /stream
        │         │                        ▼
        │    generic adapter          ┌──────────────┴──────────────┐
        │                             ▼                             ▼
        └──▶ uah/hosts/embedded/   uah/hosts/desktop/
             （UHA 进程内）          （独立 Windows HUD 进程）
```

启动方式（Phase 1 的取舍）：

- **Hub 进程"先到先得"**：谁先起来谁在 `127.0.0.1:8789` 上把 Hub 跑起来；
  UHA 运行时若发现端口已被占用，就**只作为客户端 POST**，不抢端口。
  这样用户**不需要额外启动任何东西**，HUD 也**不需要 UHA 先启动**。
- **内嵌宿主** 与 **桌面宿主** 都只是 Hub 的订阅者，画的是**同一份 `kv` 渲染中间结构**。

**"兼容适配器"策略（对应需求 §十四）**：
`ControlOverlay` 保留不动，UAH 以**旁路订阅者**身份并存。
不存在"双状态系统"，因为 `ControlOverlay` 和 UAH 都只是 `SessionController` 的读者，
**没有任何一方拥有私有状态定义**。

---

## 9. 如何避免版本不同步

| 机制 | 做法 |
|---|---|
| **同一枚举，单一定义** | `Status` 只在 `uah/core/models.py` 定义**一次**。UHA 侧**不新增任何状态 enum**，只保留已有的 `TaskPhase`（它是执行阶段，职责不同），映射表唯一存在于 `uah/adapters/uha/adapter.py` |
| **同一渲染，单一实现** | 卡片文本/颜色/进度由一个 `render_card(snapshot) -> dict` 产出；tkinter 宿主与文本宿主都只是把它画出来 |
| **协议版本独立** | `PROTOCOL_VERSION = "uah/1"`；`UAH_CORE_VERSION` / `UAH_DESKTOP_VERSION` 各自独立。宿主启动时校验 `protocol` 主版本，不匹配则**只读降级并警告**，不崩 |
| **同一仓库** | 一个 Git 历史，无法漂移 |
| **新增状态只改一处** | 加 `BLOCKED` → 只改 `models.py` 的 `Status` + `render_card` 的颜色表；UHA 与两个宿主**零改动**（有测试守这条：见 `test_status_single_source`） |

---

## 10. 与原始架构设想的冲突与调整（含理由）

### 10.1 传输方式：选 **localhost HTTP + SSE**，不选 WebSocket

需求 §八 列出"localhost WebSocket / HTTP+event stream / Named Pipe"三选一，且要求
"不依赖外部服务器、延迟低、崩溃隔离、可重连、支持多 Agent"。

**选 HTTP + SSE（Server-Sent Events）的理由**：

1. **零依赖**。stdlib 的 `http.server` + `urllib.request` 就够了。
   WebSocket 在本机没有 `websockets` 包（`.venv` 里没有），自己手写 RFC6455 握手+分帧
   是**纯粹的风险**，收益为零。
2. **SSE 天然是事件流**，不是轮询：服务端 push，客户端阻塞读一行 —— 满足"事件驱动、空闲零 CPU"。
3. **崩溃隔离天然成立**：Agent 崩溃 = HTTP 连接断，HUD 只看到 EOF，**不会跟着崩**。
4. **重连天然成立**：HUD 重连后 `GET /state` 拿全量快照，无需服务端保存会话。
5. **多 Agent 天然成立**：多路 POST 汇聚到同一个 `StateStore`。
6. Named Pipe 在 Windows 上要 `pywin32`（`.venv` 有，但 HUD 要跑在**系统 Python 3.12** 里，
   那里**没有** pywin32）—— 所以 Named Pipe 方案会**把 HUD 锁死在某个解释器上**，否决。

> 附带好处：`POST /event` 就是需求 §十 要的 `Generic Adapter` 的入口，
> 一个 `curl` 就能让任意程序接入，不需要写任何 Python。

### 10.2 桌面 HUD 的解释器：**必须能自选**（本机硬约束）

实测：

| 解释器 | tkinter | winsound |
|---|---|---|
| `E:/UnrealHybridAgent/.venv/Scripts/python.exe`（3.13.14，项目运行时） | ❌ 无 | ✅ |
| `C:/Users/PUBLIC_USER/AppData/Local/Programs/Python/Python312/python.exe`（3.12.10，系统） | ✅ 有 | ✅ |
| 托管 3.13.12 | ❌ 无 | ✅ |

**这是审计中最有实际影响的一条**：tkinter HUD 只能用**系统 Python 3.12** 跑，
而 UHA 主程序跑在 **3.13.14 的 `.venv`**。

对策（三条同时上）：
1. `uah/` 全部代码**只用 stdlib**，且**兼容 3.12 与 3.13**（不用 3.13 独有语法）；
2. `uah/tools/uah.py` 里放一个 `--python` 探测：优先找带 tkinter 的解释器；
3. **同时提供纯文本 HUD**（`hosts/desktop/text_hud.py`，无 tkinter 也能跑），
   在 `.venv` 里可用作 CI/测试与降级通道。**tkinter 缺失不是失败，是降级。**

### 10.3 状态字段：按需求 §四/§五 采纳，但做两处收敛

采纳的结构（`protocol: "uah/1"` + `event_id` + `timestamp` + `agent` + `project` +
`status` + `task{id,name,phase,stage,step,total_steps}` + `activity{summary,detail,tool}`）。

收敛：
1. `task` / `activity` **全部字段可为 `null`**（需求 §五明确要求"允许为空"）；
2. 额外加 `seq`（单调序号）与 `stale`（是否心跳过期）——
   没有 `seq` 就无法在传输层做去重与乱序保护；没有 `stale` 就无法区分
   "Agent 卡住"和"Agent 正在忙"。`elapsed_ms` 由 Hub 依据 `started_at` 计算，不由 Agent 自报。

### 10.4 **不引入** `RETRYING`

需求 §四 说"必要时允许补充 RETRYING / BLOCKED，但不要制造几十个状态"。
`RETRYING` 被**否决**：UHA 的降级重试发生在**毫秒级**且密集
（`max_attempts_per_method=2`），把它提升为 presence 状态会让 HUD 闪烁。
重试信息降级为 `activity.detail`（例如 `"attempt 2/2 · UNREAL_MCP"`），**状态仍是 RUNNING**。

`BLOCKED` **保留**：它对应"检测结论不可信、框架拒绝动手"这一类**需要人介入**的长期停滞，
与 RUNNING 有实质区别（UHA 已有 `actionable=False` 这一现成语义）。

---

## 11. 当前最小可行实现范围（Phase 1）

**做**（严格对应需求 §二十一 的开发顺序）：

1. 审计 + `UAH_ARCHITECTURE.md`（本文）✅
2. Protocol v1（`uah/core/protocol.py`）
3. Core models / state / events（`uah/core/models.py` `state.py` `events.py`）
4. UHA Native Adapter（`uah/adapters/uha/adapter.py`）
5. 接入 UHA 现有状态（`uha.py::_setup` 追加 ~6 行，guard 住）
6. 本机传输（`uah/core/transport.py`）
7. Standalone HUD MVP（`uah/hosts/desktop/`）+ 内嵌宿主（`uah/hosts/embedded/`）
8. Generic Adapter（`uah/adapters/generic/bridge.py`）
9. 完成提醒（`uah/ui/notify.py`，含 mute）
10. Test 1-10（真实运行）+ 回归（388 项基线）+ 性能记录
11. `UAH_PHASE1_REPORT.md`

**不做**（需求 §二十 明令禁止）：手机 App / 云同步 / 账号 / 多设备 / 浏览器插件 /
远控 Agent / 主题市场 / Marketplace / 数据库 / 登录 / AI 行为总结 /
Codex / Claude / Kilo 的真实集成。

---

## 12. Phase 1 验收映射（自查表）

| 需求 §二十四 | 验收项 | 落点 |
|---|---|---|
| A | UHA 有统一的机器可读状态输出 | `uah/core/protocol.py` + UHA adapter → JSON 事件 |
| B | 内嵌 UI 与 Standalone HUD 使用相同状态模型 | 两者都用 `uah/core/models.py::AgentSnapshot` |
| C | 不存在两份独立 `AgentStatus`/`TaskStatus` 定义 | `Status` 仅定义在 `models.py`；UHA 侧零新增 enum |
| D | Standalone HUD 独立于 UHA 主窗口 | `hosts/desktop/hud.py` 独立进程，只连 Hub |
| E | HUD 显示 Agent/Project/Status/Task/Stage/Step/Activity/Tool/Elapsed | `ui/components/card.py::render_card` |
| F | DONE/ERROR/WAITING_INPUT/WAITING_APPROVAL 有明显提醒 | `ui/notify.py`（视觉 + 声音 + 可选 toast，去重 + mute） |
| G | Generic Adapter 可模拟外部 Agent | `adapters/generic/bridge.py` + `POST /event` |
| H | UHA 原有能力无明显回归 | 388 项基线对比（Test 10） |


## Phase 1.1 实施更新（2026-09-21；本节替代旧版相关实现说明）

默认 Desktop 为 Compact 320×48，Expanded 320×216，原 Dashboard 为显式 Debug 模式。布局消费同一 AgentSnapshot / CardView；Status 与 StateStore 不复制。

Executor 现提供 add_progress_listener（begin/step/end/single），UAH 不再 monkeypatch run/run_plan。ApprovalGate 的策略不变；可选本地 confirmer 通过 loopback Hub 的 capability-protected approval 通道交互，默认关闭，超时/断线/重放拒绝。只有实际 confirmer 等待期间发 WAITING_APPROVAL。

UHA bootstrap / HUD 入口通过 uah.core.daemon 启动独立 Hub，Agent 退出不停止 Hub。SSE 1 秒 keepalive 使 Windows 退出有界；Hub 重启重连以新的全量快照为准，未增加磁盘历史数据库。

ControlOverlay 保留现状，Safety Banner 独立，HUD NOACTIVATE、不抬升通知，默认避开顶部 110 逻辑像素。P0 安全报告发现真实 mid-action 和 Agent/安全控制器 crash 失败，P4B 仍 BLOCKED。完整验收、命令及限制见 UAH_PHASE1_1_REPORT.md 和 docs/P0_1_REVALIDATION_REPORT.md。


## P4B/P4C 集成补充（2026-09-22）

UAH 保持同仓库一等组件，继续通过 Executor progress hook 观察，未增加第二套状态枚举。ProviderGateway 是 UHA adapters 的生命周期/诊断边界，不是 UAH Hub 的替代。独立 SendInput worker/guardian 属 P0 安全执行层，不通过 HUD 传达注入许可。旧 standalone safety daemon 禁止启动；UAH 独立 Hub 不受影响。
