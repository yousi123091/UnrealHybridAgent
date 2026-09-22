> Historical documentation: UAH is disabled in this publication snapshot and deferred to the next release.

> Public edition: project names are anonymized; historical measurements are retained.

> Publication copy: personal user/evidence paths are redacted. Historical test results are unchanged; raw evidence remains private.

# UAH Phase 1 交付报告

**Universal Agent HUD —— 通用 Agent presence / status 层**

审计与实施时间：2026-09-21 · 仓库：`<UHA_ROOT>` · 分支 `p0-cu-safety`
基线提交：`2f45122`（实施期间另一会话推到了 `0108a93`，见 §Files Changed 的说明）

一句话结论：**UHA 现在有一条统一的机器可读状态出口，它同时驱动 UHA 进程内的展示侧
和一个完全独立的 Windows HUD；两份展示读的是同一套状态机与同一套渲染函数。**

---

## 1. Architecture

### 1.1 最终架构

```
                        ┌─────────────────────────────────────────┐
   UHA Agent Core       │  src/scheduler/executor.py              │  ← 一行未改
   （不感知 UAH）        │  src/core/session_control.py            │
                        │      SessionController                  │
                        │        · snapshot()                     │
                        │        · on_change(fn)   ← 现成的钩子    │
                        └───────────────┬─────────────────────────┘
                                        │ 事件驱动（非轮询）
                                        ▼
   uah/adapters/uha/     ★ UHA Native Adapter
     adapter.py            · ControlState → UahEvent
                           · TaskPhase → Status（唯一映射表）
                           · 旁路包裹 run_plan / run / approval.evaluate
                                        │
                                        ▼
   uah/core/             ★ UAH Core（零第三方依赖）
     protocol.py           协议版本、事件类型、兼容性判定
     models.py             Status / TaskState / Activity / AgentSnapshot / Event  ← 唯一定义处
     state.py              StateStore：事件 → 快照（唯一状态机）
     events.py             EventBus（进程内 pub/sub）
     transport.py          本机 HTTP + SSE Hub：HubServer / HubClient / ensure_hub
                                        │
                        ┌───────────────┴───────────────┐
                        │                               │
                        ▼                               ▼
   uah/hosts/embedded/                    uah/hosts/desktop/
     EmbeddedHud                            HudApp（tkinter 独立进程）
     · 同一个 StateStore                    · 只连 Hub，不 import UHA
     · 同一个 render_card()                 · SSE 订阅 + 自动重连
     · 出口：UHA 结构化日志                  · 置顶小窗 / 多卡片 / 提醒 / 静音
     · 同一个 Notifier                       uah/hosts/desktop/text_hud.py（无 tkinter 降级）
                        │                               │
                        └──────── uah/ui/components/card.py ────────┘
                                  render_card(snapshot) → CardView
                                  ★ 唯一的渲染真源（纯函数，可测）
```

### 1.2 关键架构决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 子项目位置 | UHA 仓库内 `uah/`，不另起仓库 | 一个 Git 历史 = 不可能版本漂移。桌面宿主仍是独立**进程**，只有代码共享 |
| 传输 | **localhost HTTP + SSE**（stdlib） | 零依赖；SSE 是事件流不是轮询；崩溃天然隔离；重连天然成立；`POST /event` 顺带就是通用接入面。WebSocket 需要依赖（本机没有），Named Pipe 会把 HUD 锁死在某个解释器上 |
| Hub 选举 | 固定端口 **先到先得** | 用户不需要手动启动任何服务；端口被非 UAH 服务占用时**直接关掉 UAH 并记日志**，不偷偷换端口（换端口会让 HUD 连错地方） |
| 状态定义 | 唯一一份 `uah/core/models.py::Status` | 验收项 C。UHA 侧**零新增枚举**；`TaskPhase` 是执行阶段，不是第二份 presence 状态 |
| 渲染 | 唯一一个纯函数 `render_card()` | 验收项 B。两个宿主都不许自己定义颜色/标签/布局 |
| 协议版本 | `uah/1`，与 core / desktop 版本**互不比较** | 需求 §七。`UAH_DESKTOP_VERSION != PROTOCOL_VERSION`（有测试守） |
| 不引入 RETRYING 语义 | 协议保留该值，UHA 适配器从不发出 | UHA 的重试是毫秒级且密集，提升为 presence 状态只会让 HUD 闪。重试信息降级进 `activity.detail` |
| 保留 BLOCKED | 是 | 对应"检测结论不可信 / 审批无人应答"这类**必须人工介入**的长期停滞，与 RUNNING 有实质区别 |
| UHA 侧改动 | 只加 2 个受保护的旁路调用点（+18 行） | 需求 §十五：不阻塞 UHA 当前开发 |

### 1.3 与原始设想的偏离（及理由）

1. **没有为 UAH 新建独立仓库/包索引** —— 需求 §一 要的是"一个状态核心、多个宿主"，
   而不是两个项目。同仓库是达成"不漂移"最省力的方式。
2. **桌面 HUD 提供第二条降级通道（文本 HUD）** —— 本机 `.venv` **没有 tkinter**
   （实测：`.venv` 3.13.14 ❌ / 系统 Python 3.12 ✅ / 托管 3.13.12 ❌）。
   所以图形宿主必须能自动挑解释器（`hosts/desktop/launch.py`），
   并另有一条不依赖窗口系统的通路。这不是"过度开发"，是本机环境的硬约束。
3. **不把 SafetyBanner 并进协议** —— 审计发现 UHA 已有第二个 UI
   （`src/safety/banner.py`，P0 安全横幅）。它的 `SafetyState`
   （键鼠归谁）与 UAH `Status`（Agent 在做什么）是**两套不同语义**，
   合并只会互相污染。Phase 1 完全不碰它。

---

## 2. Files Changed

### 2.1 新增（全部是本任务产物）

`uah/` 子项目，**33 个 Python 文件 / 7075 行**：

| 文件 | 行 | 作用 |
|---|---|---|
| `uah/core/protocol.py` | 155 | 协议版本、事件类型、兼容性判定 |
| `uah/core/models.py` | 641 | ★ `Status` / `TaskState` / `Activity` / `AgentSnapshot` / `Event`（唯一定义处） |
| `uah/core/state.py` | 397 | ★ `StateStore`：事件 → 快照，去重 / 乱序 / 过期 / 未知状态 |
| `uah/core/events.py` | 83 | `EventBus` |
| `uah/core/transport.py` | 639 | 本机 HTTP + SSE Hub 与客户端、Hub 选举 |
| `uah/adapters/uha/adapter.py` | 798 | ★ UHA 原生适配器 + 两侧旁路埋点 |
| `uah/adapters/generic/bridge.py` | 202 | 通用适配器（一条 `curl` 即可接入） |
| `uah/adapters/publisher.py` | 111 | 发布通道：本地 / HTTP / 空实现 |
| `uah/ui/components/card.py` | 325 | ★ 唯一的卡片渲染（纯函数） |
| `uah/ui/components/tk_card.py` | 160 | 唯一的 tkinter 组件 |
| `uah/ui/notify.py` | 427 | 提醒策略（去重 / 静音 / 声音 / Windows 通知 / HUD 高亮） |
| `uah/hosts/embedded/bootstrap.py` | 363 | ★ UHA 接入引导（两个入口函数） |
| `uah/hosts/embedded/host.py` | 161 | 内嵌宿主 |
| `uah/hosts/desktop/hud.py` | 448 | ★ 独立桌面 HUD |
| `uah/hosts/desktop/text_hud.py` | 136 | 无 tkinter 降级 HUD |
| `uah/hosts/desktop/launch.py` | 141 | 自动挑带 tkinter 的解释器 |
| `uah/hosts/desktop/__main__.py` | 57 | `python -m uah.hosts.desktop` |
| `uah/tools/uah.py` | 283 | 独立 CLI：`hub / state / watch / emit / hud / notify / selftest` |
| `uah/tests/test_uah_phase1.py` | 1020 | Test 1-10 + 验收自查（152 项） |
| `uah/tests/gui_smoke.py` | 149 | 真实窗口冒烟（19 项，需 tkinter 解释器） |
| `uah/tests/perf_probe.py` | 193 | 性能实测 |
| 其余 `__init__.py` 等 | — | 包结构 |

文档：`UAH_ARCHITECTURE.md`（架构判断）、`UAH_PHASE1_REPORT.md`（本文件）。

### 2.2 修改（**刻意最小**）

| 文件 | 改动 | 说明 |
|---|---|---|
| `uha.py` | **+18 行**（2 个 try/except 块） | ① 装配前 `uah_begin_boot`（发真实的 STARTING）；② 装配后 `uah_attach`（挂观察者）。两块都整体 try/except，UAH 出任何问题只写一行日志，**不影响 UHA 主流程** |
| `config/agent.config.json` | 追加 `uah` 段 | `enabled / host / port / agent_id / phase_label / heartbeat_s / embedded / notify`。已有键值一个未改 |
| `config/agent.config.example.json` | 追加 `uah` 段 | 同上 |
| `src/scheduler/executor.py` | **1 处缩进修正** | ⚠️ **非本任务引入**，见 §2.3 |
| `UAH_ARCHITECTURE.md` | 按实测结果回填修正 | 审计文档本身 |

**没有做的事**：没有删任何 UI、没有删任何测试、没有改任何依赖版本、
没有做与本任务无关的格式化、没有 `reset` / `clean`、没有触碰 `src/safety/**` 的语义。

### 2.3 ⚠️ 两个必须说明的仓库状态问题

**(a) 审计发现 HEAD 提交本身有语法错误。**

`src/scheduler/executor.py:515` 有一整块 P0 安全代码被**多缩进了 4 格**：

```python
        is_gui = method in ("MOUSE", "KEYBOARD")
                if is_gui:              # ← IndentationError: unexpected indent
```

后果是 `import src.scheduler` 直接失败 → **整个 CLI 无法运行**
（`python uha.py demo1 ...` 崩在 import 阶段）。这**先于本任务存在**。

为了满足需求"必须真实运行、不允许只写代码不验证"，做了**最小修复：只把那一段缩进改回正确值，语义零变化**。
修复后全仓库 `py_compile` 通过（`syntax failures: 0`）。

> 该修复已被另一会话的检查点提交 `0108a93` 吸收（`git show HEAD:src/scheduler/executor.py` 现在可编译）。

**(b) 实施期间有另一个会话在并行开发同一仓库。**

证据：`git log` 从 `2f45122` 前进到 `0108a93 chore(p0.1): checkpoint before human-override architecture correction`；
工作区里出现了我未创建的改动与新文件
（`src/safety/controller.py` +151 行、`src/safety/banner.py`、`src/adapters/computer_use.py`、
`tools/p0_safety_banner_win32.py`、新增 `src/safety/{cleanup,diagnostics,human_override}.py`、
`tests/test_p01_human_override.py`）。

**这些都不是我的改动，我没有碰过它们。** 我的改动清单严格限于 §2.1 / §2.2。
我也没有覆盖或回退对方的任何工作。

---

## 2.4 实施期间发现并修掉的真实缺陷

这些都是**跑起来才暴露**的（写代码时看不出来），全部有测试或实测证据守着。
列在这里是因为它们比"功能实现了"更值得留存。

| # | 缺陷 | 表现 | 根因 | 修法 |
|---|---|---|---|---|
| 1 | `store or StateStore()` **静默丢弃注入的 store** | 注入的 store 与 Hub 真正在用的 store 是两个对象，状态"看起来对其实各记各的"，不报错 | `StateStore` 定义了 `__len__` → **空 store 是 falsy** | 改成 `store if store is not None else ...`（transport / embedded host 两处） |
| 2 | **关窗口永久卡死** | HUD 点关闭后主线程不返回；用 `faulthandler` 抓到死在 `HTTPResponse.close()` → `io.BufferedReader.close()` | 该调用要关 socket 上的缓冲文件对象，而缓冲区的锁正被**另一线程阻塞中的 `readinto()`** 持有（Windows 稳定复现） | 改调 `socket.shutdown(SHUT_RDWR)`，让阻塞的读自己返回，读线程再走 `with resp:` 收尾 |
| 3 | Hub 关闭可能被卡住的订阅者拖住 | `server.stop()` 长时间不返回 | `ThreadingMixIn` 默认 `block_on_close=True`，会 join 它起过的**所有**线程（含 daemon）；SSE 是长连接 | 设 `httpd.block_on_close = False` |
| 4 | **SSE 首次同步的竞态吞掉一条更新** | 偶发"HUD 卡在旧状态一次"，只在"订阅者接入的瞬间刚好有事件"时命中 | `snapshots()` 返回的是存储里的**可变对象**，先读内容再读签名，中间落入事件 → 发旧内容、记新签名 → 下次比较认为"没变" | 新增 `wire_snapshots()`：序列化与过期计算在**同一把锁内**完成；签名只从返回的 dict 上算 |
| 5 | **重启后的 Agent 被整段静默压制** | Agent 崩溃后重启，HUD 一直显示崩溃前那一刻的状态（一个不会报错的哑故障） | 序号保护只看 `seq` 变小，而重启后序号是**从头数**的，于是每条事件都被当成"迟到旧事件"丢弃 | 只有"序号更小 **且** 时间戳更早"才算迟到；`agent.started` 或 `seq == 1` 视为新实例并重置序号基准 |
| 6 | **计划执行期间状态横跳 → 通知轰炸** | HUD 显示 `DONE → RUNNING → DONE → RUNNING`；3 步计划会发 3 次"已完成"提醒 | UHA 的 `Executor.run()` **每一步**结束都调 `controller.end_task()`，`TaskPhase` 在中途就变 DONE | 计划执行中（`plan_begin..plan_end`）单步 DONE 折成 `RUNNING` + Stage `Between Steps`，只有 `plan_end` 才发终态 |
| 7 | 会话中任务名被单步 skill 顶掉 | 卡片每一步都换一个 Task 名，用户无法回答"它到底在做什么任务" | `task_name = skill or plan_name` 优先级反了 | 改为 `plan_name or skill`（计划是任务，当前 skill 属于 Activity 与 Step） |
| 8 | **提醒评估阻塞 UI 约 0.9 秒** | 每次完成提醒会冻住 HUD 界面近一秒 | `ToastSink.send()` **同步**起 PowerShell（实测 867ms） | 放到后台线程，同时只允许一条在飞；`consider()` 实测 0.97ms 返回 |
| 9 | 静音在无持久化路径时抛断言 | 测试/嵌入场景按静音按钮会 `AssertionError` | `set_mute(persist=True)` 无条件 `_save()`，而 `_save()` 里有 `assert state_path is not None` | 没配路径就不持久化（静音本身仍然生效），不报错 |
| 10 | 窗口尺寸不生效 / 关闭后 Tcl 报错 | 窗口退回 Tk 默认 `200x200`；关闭时刷一串 `invalid command name ..._drain` | ① `geometry()` 在 `pack` 控件**之前**设，被请求尺寸改写；② 窗口销毁后仍有排期的 `after()` 回调 | ① 把 `_place_window` 移到 `build()` 末尾；② 记录 after id 并在 `close()` 里 `after_cancel` |
| 11 | 提醒去重会漏掉"换了个问题" | Agent 在 WAITING_INPUT 下问了**另一个**问题却永远收不到提醒 —— 而那正是最需要打扰的时刻 | 去重键只有 `(agent, status, 变化时刻)`，状态没变就不算新变化 | 键里加入"具体在等什么"（activity 文案）；并把抖动抑制限定在非 needs-human 状态上 |

> 其中 #2 / #4 / #5 / #6 / #11 是**会导致错误结论或卡死**的那一类 ——
> 它们不会报错，只会让用户看到一个"看起来正常但其实错了"的 HUD。
> 这也是为什么本项目坚持"必须真实运行"，而不是"写完代码就算完成"。

---

## 3. Protocol

### 3.1 线上格式（`uah/1`）

```json
{
  "protocol": "uah/1",
  "event_id": "9f1c2e7a...",
  "seq": 42,
  "type": "task.step_changed",
  "timestamp": 1789991933.249,
  "agent":   { "id": "uha", "name": "UHA", "type": "uha" },
  "project": { "name": "ExamplePalace",
               "path": "<UE_PROJECT_ROOT>/ExamplePalace" },
  "status": "RUNNING",
  "task": {
    "id": null, "name": "demo1_raise_and_save",
    "phase": "Phase 4B", "stage": "Structured Execution",
    "step": 2, "total_steps": 3
  },
  "activity": { "summary": "actor_move", "detail": "执行中", "tool": "Unreal MCP" }
}
```

**除 `status` 外的所有语义字段都允许为 `null`**（需求 §五）。
外部 Agent 只说得出 `{"agent":"X","status":"running"}` 时，其余字段全部为 null，UAH 照样显示。

### 3.2 状态机（`Status`，12 个值，唯一定义处）

| 值 | 含义 | 分组 |
|---|---|---|
| `IDLE` | 在，但没干活 | — |
| `STARTING` | 刚起来，还在装配 | active |
| `RUNNING` | 正在干活 | active |
| `WAITING_INPUT` | 在等人给信息 | **needs_human / 提醒** |
| `WAITING_APPROVAL` | 在等人批准 | **needs_human / 提醒** |
| `PAUSED` | 被人暂停 | — |
| `ERROR` | 出错停下 | terminal / 提醒 |
| `DONE` | 干完了 | terminal / 提醒 |
| `CANCELLED` | 被取消 | terminal |
| `RETRYING` | 协议保留；UHA 适配器**从不发出** | active |
| `BLOCKED` | 卡住，必须人工介入 | **needs_human / 提醒** |
| `UNKNOWN` | 认不出来的状态。UI 照样显示 | — |

解析**永不抛异常**：认不出的字符串 → `UNKNOWN`；别名齐全
（`running/working/busy/active/in_progress` 都 → `RUNNING`；`done/complete/success/ok/passed` 都 → `DONE` …）。

### 3.3 事件类型

`agent.started` · `agent.stopped` · `agent.heartbeat` ·
`task.started` · `task.stage_changed` · `task.step_changed` · `task.completed` ·
`task.failed` · `task.cancelled` · `activity.changed` ·
`approval.required` · `user_input.required` · `status.changed`

未知事件类型**照收不误**（记进快照的 note，不触发特殊语义）——
这样以后加类型时，老 Hub 不会把新 Agent 判成"非法"。

### 3.4 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/health` | 存活 + 协议版本 + Agent 计数（`HubClient.is_alive()` 用它探活） |
| `GET` | `/state` | 全量快照（HUD 重启后靠它立刻恢复） |
| `GET` | `/stream` | SSE：先全量，再增量，15s 心跳 |
| `POST` | `/event` | 摄入一条/一批事件。也接受 `{"events":[...]}` |

### 3.5 版本策略

```
UAH Protocol  1.0     ← 线上格式，主版本不兼容才拒绝（次版本只加不删）
UAH Core      0.1.0   ┐
UAH Desktop   0.1.0   ├ 各自独立演进，**不与协议版本绑定**
UHA           x.x     ┘
```
所以 `UHA 0.8` 与 `UAH Desktop 1.4` 只要都认 `uah/1` 就能通信。
协议不兼容时**不崩**：收下、标记 `protocol_ok=false`，卡片页脚显示"协议不兼容"（有测试守）。

---

## 4. UHA Integration

### 4.1 接入点（只有两处，都在 `uha.py::_setup`）

```python
log = RunLogger(...)

# ① 装配前：发一条**真实**的 STARTING（build_bundle 去连 MCP / Computer Use 是真的要几秒）
try:
    from uah.hosts.embedded.bootstrap import uah_begin_boot
    uah_boot = uah_begin_boot(cfg, log)
except Exception as exc:
    log.event("uah_boot_error", error=str(exc)[:200])

bundle = build_bundle(cfg)
executor = Executor(bundle, cfg, log)
...（原有的 safety / overlay / hotkey 装配，一行未改）...

# ② 装配后：挂观察者
try:
    from uah.hosts.embedded.bootstrap import uah_attach
    uah_attach(cfg, log, executor, boot=uah_boot)
except Exception as exc:
    log.event("uah_attach_error", error=str(exc)[:200])

return bundle, log, executor
```

### 4.2 状态是怎么被读出来的（**零轮询**）

UHA 早就有现成的观察者钩子 `SessionController.on_change(fn)`，直接复用：

```python
controller.on_change(self._on_change)     # 事件驱动
```

`_on_change` 里**只做映射与入队**，绝不做 I/O。因为 `on_change` 是在
`SessionController` 的锁内同步触发的 —— 在那里做 HTTP 会拿 UHA 的会话锁去等网络。
所以所有事件先进本对象的**出站队列**（上限 512，满了丢最旧的并计数），由独立线程发送。

### 4.3 唯一映射表（`TaskPhase` → `Status`）

```python
UHA_PHASE_TO_STATUS = {
    "IDLE": Status.IDLE,
    "ANALYZING": Status.RUNNING,
    "STRUCTURED_EXECUTION": Status.RUNNING,
    "COMPUTER_CONTROL": Status.RUNNING,
    "VERIFYING": Status.RUNNING,
    "PAUSED": Status.PAUSED,
    "STOPPING": Status.RUNNING,      # 还在收尾，没结束
    "ABORTED": Status.CANCELLED,
    "DONE": Status.DONE,
    "FAILED": Status.ERROR,
}
```

这张表把 UHA 的**实现细节**（四种"正在干活"的方式）收敛成一个对外的 `RUNNING`。
有一条测试要求它**覆盖 `TaskPhase` 的全部成员且没有多余残留键** —— 以后新增阶段忘了映射会被抓住。

### 4.4 三处**旁路埋点**（行为零改动）

| 埋点 | 手法 | 不改什么 |
|---|---|---|
| Step i / N | 包裹 `executor.run_plan` / `executor.run` | 返回值原样透传、异常原样抛出 |
| WAITING_APPROVAL | 包裹 `executor.approval.evaluate` | **决策与返回值原样返回** —— UHA 的审批语义与 UAH 是否存在完全无关 |
| WAITING_INPUT | 适配器公开 `notify_waiting_input()`；`_confirmer` 存在时先报等待再问人 | 不安装任何 confirmer，不改变谁能批准 |

为什么用"包裹"而不是改 Executor：Executor 是 40k 的核心闭环，
而 Step i / N 这个信息在**边界上**就能拿到。这与 `uha.py` 里已有的
`executor.approval._confirmer = ...` 是同一手法。

### 4.5 集成暴露并修掉的两个真实问题

**(a) 计划执行期间状态在 DONE/RUNNING 之间横跳。**
实测发现 UHA 的 `Executor.run()` **每一步结束都会 `controller.end_task()`**，
于是 TaskPhase 在计划中途就变成 DONE。直接照搬的后果是 HUD 显示
`DONE → RUNNING → DONE → RUNNING`，**并且每一步都触发一次"已完成"提醒**——
正是需求 §十三 明令禁止的弹窗轰炸。

修法（语义上更正确）：**计划 = 任务。计划没跑完，任务就没完成。**
计划执行中（`plan_begin..plan_end`）单步 DONE 折成 `RUNNING` + Stage `Between Steps`，
只有 `plan_end` 那一刻才发真正的终态。实测序列（真实 `uha.py demo1`，3 步计划）：

```
STARTING → IDLE → RUNNING(step 1/3) → RUNNING(2/3) → RUNNING(3/3) → DONE    ← 只有 1 个 DONE
```

**(b) 会话里任务名会被单步 skill 顶掉。**
原实现 `task_name = skill or plan_name` 会让卡片在每一步都换一个 Task 名，
用户就没法回答"它到底在做什么任务"。改为 `plan_name or skill` ——
计划是任务，当前 skill 属于 Activity 与 Step 计数。

### 4.6 UHA 自己的日志里也能看到状态

内嵌宿主把每条状态变化写进 UHA 现有的结构化日志（`logs/runs/<run>.jsonl`）：

```json
{"kind": "uah_presence", "agent": "UHA", "status": "RUNNING",
 "task": ["Phase 4B · actor_find"], "stage": ["Analysis"], "step": ["1 / 3"],
 "activity": ["路由中"], "tool": [], "elapsed": "elapsed 0:01"}
```

---

## 5. Embedded UI

**结论：UHA 内嵌的展示侧已经使用共享状态源，而且是同一个对象。**

`uah/hosts/embedded/host.py::EmbeddedHud` 是**发布链上的一层包装**：

```
adapter ──▶ EmbeddedHud ──▶ 真正的 Publisher ──▶ Hub ──▶ 桌面 HUD
                │
                ├─▶ StateStore.apply()   （同一套状态机）
                └─▶ render_card()        （同一套渲染）
                        │
                        ├─▶ UHA 结构化日志（logs/runs/*.jsonl，"uah_presence"）
                        ├─▶ 控制台单行（可选，默认关）
                        └─▶ Notifier（与桌面宿主同一套提醒策略）
```

所以"内嵌 UI 用的状态"与"桌面 HUD 用的状态"**在代码层面就是同一个对象**。
有一条测试直接断言 `embedded_mod.render_card is card_mod.render_card`。

### 关于原有的 `ControlOverlay`

**保留、未改、未删**（需求 §十四）。关系说清楚：

- `ControlOverlay` 读 `SessionController.snapshot()`（UHA 内部状态）；
- `EmbeddedHud` 读 `uah.core.StateStore`（同一份状态的对外投影）；
- 两者都是 **`SessionController` 的读者**，**没有任何一方拥有私有状态定义**。

因此"双状态系统"不成立。之所以没有把 `ControlOverlay` 也改成读 `AgentSnapshot`：
(a) 需求明说不删原有 UI；(b) 本机 `.venv` 没有 tkinter，那个窗口本来就没起来过，
改它属于"改一个跑不到的路径"，风险大于收益；(c) 真正强制共享的是
**状态定义与渲染函数**，这两者已经做到了（验收项 B / C）。

### 一个必须写下来的限制

tkinter 的 `mainloop()` **必须**在主线程，而 UHA 的主线程要给 Executor。
所以内嵌宿主**不自己开窗口** —— 它复用同一个 Hub 订阅 + 同一套组件，
把状态送到 UHA 自己的展示通道。这条注释也写在了 `hosts/desktop/hud.py` 末尾。

---

## 6. Desktop HUD

`uah/hosts/desktop/hud.py`，独立进程，**不 import UHA 任何代码**（有测试守）。

| 需求 §十一/§十二 | 实现 | 实测证据 |
|---|---|---|
| Always on top | `root.attributes("-topmost", True)`，窗口内可一键切换 | `attributes("-topmost") == 1` |
| 小尺寸 / 低干扰 | 320 宽 × ≤640 高，默认右上角 | 实测 `320x640` |
| 拖动 | 原生标题栏 + 按住卡片头部拖动 | — |
| 最小化 / 关闭 | 原生标题栏 + 窗口内 `×` 按钮 | `窗口已关闭` = True |
| 多 Agent 卡片 | 任意数量，按"需要人看的排前面"排序 | 两张卡片 `['Codex','UHA']` |
| Agent 名称 / 项目 / 状态 | 卡片标题 + 徽标 + 副标题 | 卡片标题各自正确 |
| Task / Stage / Step / Activity / Tool | `render_card` 的 rows | 5 类字段全部命中 |
| elapsed time | 秒级时钟；**跑完就停在完成时刻** | `elapsed 1:35 → 1:45`（跑）/ 恒定（已结束） |
| DONE / ERROR / Waiting Input / Waiting Approval | 状态徽标 + 边框色 | `#3fb950` / `#f85149` / `#d29922` / `#f0883e` |
| Agent 消失 | 标灰 + "离线 Ns"，**不立刻删卡** | `离线 1s` |
| Agent 优雅退出 | 保留最后状态 + "已退出" | `已退出` |
| 完成提醒 | 闪边框 + 抬升窗口 + 提示音 + 可选 Windows 通知，**可静音** | 见 §7 |
| 崩溃隔离 | SSE 断流 → 自动重连 → 拿全量快照 | `connected / disconnected / connected` |

**它只知道 `uah/1` 协议和 `http://127.0.0.1:8789`，不知道 Unreal 是什么。**

### 卡片实际长什么样（文本通路实测输出）

```
✓ DONE  SelfTest
    generic
    Task     Running tests
    Activity pytest
    elapsed 0:00
```

### 提醒

`uah/ui/notify.py`。三条硬要求逐条落地：

1. **要提醒** —— `DONE / ERROR / WAITING_INPUT / WAITING_APPROVAL / BLOCKED`
   从非提醒态变成提醒态时触发。
2. **同一个状态变化只提醒一次** —— 去重键 `(agent_id, status, 状态变化时刻, 具体在等什么)`。
   一条测试专门守这条；并且**刻意**让"换了个问题"仍能重新提醒
   （只顾去重会让"Agent 问了另一个问题"永远收不到提醒 —— 那是最需要提醒的时刻）。
3. **一定有 mute** —— `Notifier.set_mute()`，持久化到 `.state/uah/notify.json`，
   重启后依然静音。另有"同一状态下 4s 内不重复响"的抖动抑制，但**不拦** needs-human 类状态。

通道（任一可用即可，全部不可用也不影响 HUD 显示）：

| 通道 | 实测状态 |
|---|---|
| HUD 自身高亮（闪边框 + 抬升窗口） | ✅ 始终可用，`available: True` |
| `winsound` 提示音 | ✅ 可用（按严重级选不同 SystemSound） |
| Windows 通知（PowerShell WinRT toast） | ✅ **实测可用**（`returncode == 0`，单条约 0.9s，**已放到后台线程**，失败一次即永久禁用不再重试） |

**不做的**：不弹模态框、不重复响、不因为提醒失败影响状态流。

---

## 7. Generic Adapter

外部 Agent 的接入面**一条 HTTP 请求**：

```bash
curl -X POST http://127.0.0.1:8789/event \
     -d '{"agent":"ExampleAgent","status":"running","task":"Running tests","activity":"pytest"}'
```

或等价地用自带 CLI：

```bash
python -m uah.tools.uah emit --agent ExampleAgent --status running \
       --task "Running tests" --activity pytest
```

Python 侧：

```python
from uah.adapters.generic.bridge import GenericBridge
b = GenericBridge("http://127.0.0.1:8789")
b.emit(agent="ExampleAgent", status="running", task="Running tests", activity="pytest")
b.emit(agent="ExampleAgent", status="done")
```

宽容的地方（都是为了少写代码）：
`agent` 可以是字符串或对象；`task` / `activity` 可以是字符串或对象；
缺 `protocol` / `event_id` / `timestamp` / `type` 由 UAH 补齐；
`status` 认不出来不报错（记 `UNKNOWN` 并显示）。

**不宽容的地方**：绝不替外部编造它没说的 `stage` / `step` / `tool`。
"我不知道"显示成空，比显示一个编出来的值好（有测试守）。

`event_id` 缺失时会**按内容合成**一个稳定 id —— 所以"同一条事件重复发送"会被去重，
而不是每发一次都被当成新事件。

---

## 8. Tests

全部**真实执行**，命令与结果如下（不是"应该可以运行"）。

### 8.1 主测试套件

```bash
<UHA_ROOT>/.venv/Scripts/python.exe uah/tests/test_uah_phase1.py
```

**结果：全部通过（152 项），用时 49.1s / 48.3s（连跑两次均通过）。**

| 需求 §十七 | 实测内容 | 结果 |
|---|---|---|
| **Test 1** UHA 启动 IDLE→STARTING→RUNNING | 用**真实** `SessionController`；HUD 侧通过 SSE 观察到顺序 `['STARTING','IDLE','RUNNING']` | ✅ 5 项 |
| **Test 2** Task 状态变化 → DONE | Step `1/3 → 2/3 → 3/3`、Stage `Analysis → Structured Execution → Verification → Finished`、Tool `Unreal MCP`；**并断言计划中途不出现 DONE** | ✅ 16 项 |
| **Test 3** WAITING_INPUT 提醒 | HUD 看到 WAITING_INPUT + 提醒标题「等待你的输入」+ 内容含具体问题；重复上报不重复提醒；静音有效且不影响状态；**换问题会重新提醒** | ✅ 17 项 |
| **Test 4** WAITING_APPROVAL 提醒 | 走**真实 `ApprovalGate`**；断言"审批者被问的那一刻 HUD 上已经是 WAITING_APPROVAL"；无人应答时标 WAITING_APPROVAL + BLOCKED 且**决策仍是 REQUIRE_CONFIRMATION**（证明 UHA 行为未被改） | ✅ 12 项 |
| **Test 5** ERROR 显示错误、UI 不崩 | ERROR 卡片边框 `#f85149`；灌 11 类烂数据（非对象 / 空对象 / 乱码状态 / 字段类型全错 / 列表当 task / 5KB 字符串 / 毫秒时间戳 / `uah/0` 旧协议 / 未知事件类型 / payload 是列表 / 截断 JSON）全部被吸收，Hub 存活、状态可读、全部快照都能渲染 | ✅ 24 项 |
| **Test 6** HUD 关闭再启动 | 停掉 HUD → **在新 HUD 上不产生任何新事件**的情况下，立刻拿到全量状态（RUNNING / PlanX / 2 of 5 全部保留） | ✅ 6 项 |
| **Test 7** UHA 崩溃 / 重启 | 崩溃（**不发 `agent.stopped`**）→ 1.3s 后标 `stale` 且保留 RUNNING、卡片显示「离线 1s」；重启（同 agent_id、序号从头）→ HUD 拿到新状态、stale 清除；对照：优雅退出显示「已退出」且终态不会过期。**另外验证 Hub 重启后 HUD 自动重连**（`connected → disconnected → connected`）并收到新事件 | ✅ 13 项 |
| **Test 8** 两个 Agent 同时连接 | HUD 收到 2 个、渲染 2 张卡片、标题各自正确、id 不重复、Codex 显示 WAITING_APPROVAL 与项目/任务 | ✅ 6 项 |
| **Test 9** Generic Adapter | 最简载荷 accepted；`task`/`activity` 字符串正确落位；**没有编造 stage/step/tool**；RUNNING → DONE；状态别名可解析 | ✅ 8 项 |
| **Test 10** UHA 原有能力无回归 | 见 §8.2 | ✅ 9 项 |
| **验收自查 A-H** | 见 §8.3 | ✅ 35 项 |
| **内嵌宿主共享状态** | 内嵌宿主拿到快照、写出 `uah_presence`、能渲染文本卡片、与桌面宿主同源、事件同时转发下游 | ✅ 5 项 |

### 8.2 Test 10 —— UHA 原有回归（真实子进程运行）

```
test_offline.py     exit=0   全部通过（248 项）
test_router.py      exit=0   通过 25 项，ROUTER TESTS OK
test_phase2.py      exit=0   全部通过（85 项）
test_phase3.py      exit=0   test_phase3: 11/11
test_phase4.py      exit=0   test_phase4: 13/13
test_phase4b.py     exit=0   test_phase4b: 6/6
------------------------------------------------
合计 388 项，与实施前基线完全一致
另：UAH_ENABLED=1 与 UAH_ENABLED=0 两种情况下 `uha.py list` 均正常
```

> 说明：388 项这个数字在实施前后**完全一致**（248/25/85/11/13/6），
> 且所有测试脚本没有一行改动。这就是"无明显回归"的证据。

### 8.3 验收自查（需求 §二十四 A-H）

| | 验收项 | 实测 |
|---|---|---|
| **A** | UHA 有统一的机器可读状态输出 | ✅ `uah/1` JSON 事件 + `GET /state` + SSE |
| **B** | 内嵌 UI 与 Standalone HUD 使用相同状态模型 | ✅ `embedded_mod.render_card is card_mod.render_card` 为真；两宿主都从 `ui.components.card` 取颜色，都不自定义颜色表 |
| **C** | 不存在两份独立 `AgentStatus`/`TaskStatus` 定义 | ✅ 全仓库扫描 `class .*Status.*Enum`：只有 `uah/core/models.py` 一处；且断言 UHA `TaskPhase` 全部有映射、映射表无多余键 |
| **D** | Standalone HUD 独立于 UHA 主窗口 | ✅ 独立进程；测试断言 `hud.py` 里没有 `from src.` / `import src.`；只有 `embedded/bootstrap.py` 碰 UHA 源码 |
| **E** | HUD 能显示 Agent/Project/Status/Task/Stage/Step/Activity/Tool/Elapsed | ✅ 卡片逐项命中 9 个 token；徽标以 `●` 开头、进度比 = 3/7；elapsed 跑的时候涨、结束后停 |
| **F** | DONE/ERROR/WAITING_INPUT/WAITING_APPROVAL 有明显提醒 | ✅ 四者都在提醒集合；DONE 触发提醒、同一变化只提醒一次、mute 生效、取消静音后恢复 |
| **G** | Generic Adapter 可模拟外部 Agent | ✅ 见 Test 9 |
| **H** | UHA 原有能力无明显回归 | ✅ 388 项一致 |

### 8.4 其他真实运行

**协议自检**（起 Hub → POST → SSE 收 → 校验）：
```bash
.venv/Scripts/python.exe -m uah.tools.uah selftest
→ 自检：11/11 通过
```

**真实窗口冒烟**（必须用带 tkinter 的解释器）：
```bash
"<PYTHON_EXE>" uah/tests/gui_smoke.py
→ 窗口冒烟：全部通过（19 项）
```
覆盖：窗口真的建出来（`320x640`）、置顶生效、两张卡片建出且挂进窗口树、
WAITING_APPROVAL 用告警边框色、DONE 更新、提醒有记录、静音按钮双向切换、干净关闭。
**关闭时排期的 `after()` 回调已被取消**（否则 Tk 会报一串 `invalid command name`）。

**真实 UHA CLI 端到端**（`--dry-run`，不需要 UE）：
```bash
.venv/Scripts/python.exe uha.py demo1 --actor Hall_Floor --delta 20 --dry-run
```
`uah_presence` 序列（从 UHA 自己的 JSONL 日志里读出来的）：
```
STARTING → IDLE → RUNNING(1/3) → RUNNING(2/3) → RUNNING(3/3) → DONE → 已退出
```
这条同时证明了 **monkeypatch 的计划进度埋点在真实 Executor 上生效**。

---

## 9. Performance

```bash
.venv/Scripts/python.exe uah/tests/perf_probe.py
```

| 指标 | 第 1 次 | 第 2 次 | 说明 |
|---|---|---|---|
| **idle CPU** | **0.00 %**（6s 内 CPU 时间 0.0 ms） | **0.00 %** | Hub + 1 个 SSE 订阅者、无任何事件。事件驱动，空闲真的是 0 |
| **running CPU** | 1.86 %（20 事件/s） | 3.72 %（20 事件/s） | 远高于 UHA 真实频率（一次任务也就几十条事件）。第 2 次偏高是因为机器上有并行会话在编译 |
| **内存（WorkingSet）** | 35.8 MB | 35.4 MB | 与基线（35.4 MB）几乎相同 → UAH 自身开销可忽略（其中绝大部分是解释器基线） |
| **事件延迟**（POST→SSE 收到，含 HTTP 往返） | 中位 **2.41 ms** / P95 22.9 ms / 最大 27.6 ms | 中位 13.6 ms / P95 23.1 ms | 120 次采样 |
| **render_card() 单次** | — | **0.004 ms** | 2000 次平均。所以"每秒重画所有卡片"也不构成开销 |
| **事件吞吐** | 15.8 事件/s（限速 20/s 下的实测） | 17.1 事件/s | 由测试脚本自身 sleep 限速 |

**结论**：idle 零 CPU、无文件轮询、延迟毫秒级 —— 满足需求 §十八。
`ToastSink` 起 PowerShell 单条约 0.9s，**已确认放到后台线程**（`consider()` 实测 0.97ms 返回），
不会卡 HUD 界面。

---

## 10. Known Limitations

按"影响"排序，都是实测确认过的事实，不是猜测。

1. **`WAITING_INPUT` 在 UHA 侧没有真实触发点。**
   适配器提供了 `notify_waiting_input()` 且测试覆盖了完整链路，但 UHA 当前的执行流程里
   **没有任何地方会停下来等用户输入**，所以这个状态实际不会从 UHA 自发出现。
   `WAITING_APPROVAL` 有真实触发点（`ApprovalGate`），不是这个问题。

2. **没有交互式审批通道。**
   `CONFIRM` 模式下如果没配 `confirmer`，`ApprovalGate` 会判 `REQUIRE_CONFIRMATION`，
   任务被判失败。UAH **如实**把它标成 `WAITING_APPROVAL` + `BLOCKED`（并在提醒里写明
   "无人应答"），但**不能替用户批准** —— 那属于"远程控制 Agent"，是需求 §二十 明令禁止的。
   所以现在的行为是"看得见但解不开"，这一条要在 Phase 2 用本地审批 UI 解决。

3. **内嵌宿主不开窗口。**
   tkinter 的 `mainloop()` 必须在主线程，而 UHA 的主线程要给 Executor。
   所以 UHA 进程内的展示出口是**结构化日志 +（可选）控制台单行**，不是浮窗。
   真正的图形 HUD 必须另起进程。这是技术约束，不是省事。

4. **计划进度依赖 monkeypatch。**
   `executor.run_plan` / `executor.run` 被包裹以取得 Step i / N。
   我选择不改 Executor（40k 行核心闭环），但这个手法的代价是：
   如果将来有人给 Executor 换了方法名或用 `functools.wraps` 重置属性，
   埋点会静默失效（表现为 Step 显示成空，但状态仍然正确）。
   正确的长期解法是在 Executor 里加一个正式的进度钩子。

5. **`ControlOverlay` 没有改成读 `AgentSnapshot`。**
   本机 `.venv` 没有 tkinter，那个窗口从来没起来过，改它等于改一条跑不到的路径。
   目前两个展示侧共享的是**状态定义与渲染函数**（验收项 B/C 已满足），
   但"`ControlOverlay` 直接消费 UAH 卡片"这件事没有做。
   另：这意味着 §十四 说的"逐步把 UHA 内嵌 UI 改成 UAH 驱动"只完成了第一步。

6. **Hub 是"先到先得"的单点。**
   短命的 `uha run ...` 进程会成为 Hub 并在自己退出时把 Hub 一起带走 ——
   桌面 HUD 在那之后只能看到一个空的 Hub（或连不上）。
   持久化用法要先起 `python -m uah.tools.uah hub`（或让 `uah hud` 自动拉起独立 Hub）。
   这个取舍是刻意的（不引入常驻服务/开机自启），但确实是个体验缺口。

7. **`stale` 阈值是全局固定的。**
   `stale_after_s = 20s`（心跳 10s）。一个真的在跑长任务、又忘了发心跳的 Agent
   会被显示成"离线"——虽然最后已知状态仍然保留，不会误导成"完成了"。

8. **`RETRYING` / `BLOCKED` 的语义边界偏粗。**
   `BLOCKED` 目前只在"审批无人应答"和"审批被拒"两种情况下被标出。
   UHA 里更丰富的"检测结论不可信 → 拒绝动手"（`actionable=False`）**没有**接进 UAH ——
   那需要读 `RunLogger` 的结构化事件，超出 Phase 1 的观察边界。

9. **没有做 Windows 通知的图标/点击行为**，也没有通知分级策略
   （`DONE` 和 `ERROR` 除了声音不同，视觉上一样重）。

10. **性能数字带噪声**：第 2 次的 running CPU 3.72% 明显高于第 1 次的 1.86%，
    原因是实施期间仓库里有另一个会话在跑编译/测试。idle 的 0.00% 两次一致，更可信。

11. **GUI 进程采用确定性退出（`os._exit`）。**
    Windows 上 tkinter 进程在**解释器收尾**阶段（Tcl 线程终结 / 已销毁窗口的控件析构）
    可能长时间不返回 —— 表现为"窗口已经关了、进程还在"。
    产品入口 `uah.hosts.desktop.__main__` 与测试 `gui_smoke.py` 都在**显式收尾之后**
    （取消 after 回调 → 断 SSE → 落盘静音配置 → 销毁窗口）直接 `os._exit`。
    这是 tkinter GUI 的常规做法，代价是跳过了 Python 的模块级析构 —— 
    所以任何新的"必须在退出时落盘"的东西，都必须加进 `HudApp.close()`，不能依赖 `atexit`。

12. **`session_control.py` 的 `TaskPhase` 只有 10 个值，且都是"执行阶段"。**
    UAH 没有、也不应该去扩充它。如果以后 UHA 想让 HUD 显示更细的阶段
    （例如"环境感知 / 构件巡检"），应该在 UHA 侧新增一个**独立的**阶段字段并通过
    `activity.detail` 或新增的 `task.stage` 传出来，**不要**往 `TaskPhase` 里塞语义
    （那会让 `ControlOverlay`、安全层、测试全都受影响）。

---

## 11. Next Phase（建议，**未开发**）

按"收益 / 成本"排序。以下都**没有动手**。

1. **本地审批通道**（解决 Limitation 2，也是唯一让 `WAITING_APPROVAL` 变得有用的东西）
   在 HUD 上给等待批准的卡片加一个"批准 / 拒绝"按钮 → 走一条**只在本机、只对
   `CONFIRM` 模式生效**的回执通道 → `ApprovalGate._confirmer` 从"等 stdin"改成
   "等 UAH 回执，超时即默认拒绝"。要点：**默认拒绝**、带 req id 防重放、
   并且必须在 UHA 侧有开关（不能默认打开）。

2. **把 Executor 的进度改成正视钩子**（解决 Limitation 4）
   在 `Executor` 里加一个 `on_progress(step_index, total, skill)` 回调，
   UAH 适配器改订阅它，去掉 monkeypatch。改动很小，但能让埋点不再是"隐式契约"。

3. **`ControlOverlay` 换成 UAH 卡片渲染**（解决 Limitation 5，落实需求 §十四 的第二步）
   让 `ControlOverlay` 直接消费 `render_card()`，实现"UHA 内嵌 UI 也由 UAH 驱动"。
   前提是先做一个**带 tkinter 的项目 venv**（见第 4 条），否则改完也验证不了。

4. **给项目 venv 补上 tkinter，并提供跨解释器的统一入口**
   现在最别扭的一点是：UHA 跑在无 tkinter 的 3.13 `.venv`，而 HUD 必须用系统 3.12。
   建议在 `agent.config.json` 里显式声明 `uah.python`（图形宿主用的解释器），
   并在 `uha doctor` 里报告"哪个解释器能画窗口"。

5. **持久 Hub 化**（解决 Limitation 6）
   `uah hub --daemon`（Windows 下用 `pythonw` 或计划任务）＋ HUD 启动时自动检测并拉起。
   或者更简单：让 `uah hud` 在发现没有 Hub 时**先起一个独立 Hub 进程**（现在已实现一半）。

6. **接入 UHA 的 `actionable=False` 语义**（解决 Limitation 8）
   让 `BLOCKED` 真正表达"框架拒绝动手"，而不是只在审批路径上出现。

7. **健康度/统计面板**：把 Hub 的 `store.stats()`、重复/丢弃计数、各 Agent 在线时长
   做成 HUD 的一个折叠面板。这条纯属好用，不是必须。

8. **协议次版本演进机制**：目前 `uah/1.0` 只加了不删的约定，还没有"字段废弃"的正式流程。
   等在真实外部 Agent 上跑过一轮再定。

**不建议做**（至少现在还不行）：手机 App、云同步、账号、多设备、浏览器插件、
远程控制 Agent、主题市场、Agent Marketplace、数据库、登录、AI 自动总结 Agent 行为、
Codex / Claude / Kilo 的完整集成 —— 都是需求 §二十 明确划到"以后"的东西。

---

## 附：常用命令速查

```bash
# 起一个独立的 Hub（HUD 独立于 Agent 的前提）
python -m uah.tools.uah hub

# 起桌面 HUD（自动挑带 tkinter 的解释器）
python -m uah.tools.uah hud
python -m uah.tools.uah hud --text        # 无 tkinter 时用文本 HUD

# 看当前状态 / 跟随
python -m uah.tools.uah state
python -m uah.tools.uah watch

# 模拟一个外部 Agent
python -m uah.tools.uah emit --agent Demo --status running --task "Running tests" --activity pytest
python -m uah.tools.uah emit --agent Demo --status done

# 提醒开关
python -m uah.tools.uah notify mute | unmute | status

# 自检 / 测试 / 性能
python -m uah.tools.uah selftest
python uah/tests/test_uah_phase1.py          # 152 项（.venv 即可）
python uah/tests/gui_smoke.py                # 19 项（需带 tkinter 的解释器）
python uah/tests/perf_probe.py               # 性能

# 让 UHA 跑起来时顺便把状态报出来（默认开启，可用环境变量彻底关掉）
python uha.py demo1 --actor Hall_Floor --delta 20 --dry-run
UAH_ENABLED=0 python uha.py doctor            # 完全禁用 UAH
```
