> Publication copy: personal user/evidence paths are redacted. Historical test results are unchanged; raw evidence remains private.

> Publication copy: personal user/evidence paths are redacted. Historical test results are unchanged; raw evidence remains private.

# P0.1 真实性复验报告

## 2026-09-22 最终安全收尾（本节为最新结论）

本机受支持路径 P0 验收 PASS；P4B 安全阻塞已解除（UNBLOCKED）。本机 v0.1 为 READY WITH KNOWN ISSUES；公开发布未批准、历史秘密审计未完成。已完成三项真人适配器链路验收，后续 hook/资源清理增强通过 76 项真实 OS 故障与清理检查、完整适配器自动回归及十组全回归。140 Python 文件 syntax failures=0。

完整修复、失败保留、准确命令、有限延迟及未验证边界见 P0_1_FINAL_VALIDATION_REPORT.md。以下 BLOCKED/NOT READY 是此前阶段历史结论，不再代表本节定义的本机受支持范围。


## 完整适配器链路真人验收完成（2026-09-22，最新）

**三项 PASS：鼠标 mid-drag、键盘 mid-drag、held Shift 接管。** 本次实际经过公开 ComputerUseAdapter API → 真实 Agent-TARS HTTP 服务租约 → 生产 guarded input worker/guardian，配合 SafetyController 与 Win32 Safety Banner。服务 dryRun=false。用户对全部三项在对话确认“均正常”。

三项均在动作期间捕获无 injected 标志的真实物理事件，进入 HUMAN_OVERRIDE、释放鼠标按钮/Shift、拒绝后续输入及三次自动重开。服务端独立 status 读回租约 holder=null；release 后仍保持 HUMAN_OVERRIDE。worker 退出码均为 0，持有输入账本清空。Shift 场景在接管前由 OS 查询确认实际按下。

原始窗口结果中，第二、三项因未点击窗口反馈而保留 PENDING HUMAN VALIDATION；本次根据用户在对话中的明确“均正常”及上述机器证据，另存 reviewed-results.json 判为 PASS。原始日志不改写，不伪造窗口反馈。

此前“完整适配器链路缺少真人接管验收”的缺口已经关闭，不再要求重复这三项。范围是本机空白测试窗口、当前版本及真实独立服务实例；不是多客户端竞争、UE 插件操作或所有应用场景的通用认证，也没有宣称零延迟。

整体 P4B 仍按原有其他未满足的安全项保持 BLOCKED：hook 超时/unhook 返回诊断、此前列明的故障组合及压力边界尚未闭环。这些是软件验证工作，不把本次已经完成的人工项继续列为 pending。v0.1 仍 NOT READY，其他功能/公开发布限制不因人工接管通过而自动解除。

本次证据位于 human-validation-20260922/full-adapter-chain，包含原始/复核结果、真实服务预检、各 worker 与 guardian 清理记录。先前失败和分段测试均保留。


## 2026-09-22 真人验收补充（最新）

用户亲自完成新版 guarded worker 的鼠标 mid-drag、键盘 mid-drag、held Shift 三项接管；物理 hook、OS 释放结果、自动重开拒绝和用户反馈一致，三项 PASS。原 pending 在这些限定范围内解除。

本次未经过远端 ComputerUseAdapter 租约链路，不能自动宣称整条链路人工通过；P4B 仍 BLOCKED、v0.1 NOT READY。完整范围、首次 banner 准备失败及原始证据见 P0_1_HUMAN_ACCEPTANCE_REPORT.md。以下保留先前历史结论。


## 2026-09-22 后续实现复验（优先于下方历史结论）

本轮 P4B 仍 BLOCKED，总体 PARTIAL。代码中原终态锁存、禁止 request_control 自动重开、fail-closed、注入标志归因、banner-first、心跳、forced release 参数回归通过。旧 nut-js 动作不再承担 UHA 输入；不修改第三方 backend，以本机 guarded SendInput worker + 独立 guardian 替代，并保留 Agent-TARS 租约/截图。

- PASS（自动/真实 OS）：10 次完整 start-finish，F24 真正按下和释放、worker 正常退出。
- PASS（真实故障）：Agent/Controller 所在 owner 进程、输入 worker、guardian 分别终止，OS 查询显示 F24 释放，最后一次测量约 11–16 ms。controller 与 Agent 同进程，不能冒称另一个独立 controller process 也被单测。
- PASS（限定自动）：真实画布拖拽期间模拟撤销；故意冻结共享 gate 为 ALLOWED，独立信号仍中止，按钮释放且后续光标不动。模拟不是 HUMAN VERIFIED。
- FAIL（保留历史失败）：补强前某次 mid-drag 未及时中止。新增取消信号后上述故障注入重跑通过，失败日志仍保留，不宣称原因已唯一定位。
- PARTIAL：ledger-driven release 生存期已改善；SendInput 最终检查与实际注入之间仍存在 OS 调度窗口。双重故障、磁盘同时故障、低层 hook 超时退出未全覆盖；旧 detector unhook 的返回诊断尚不足。
- PENDING HUMAN VALIDATION：新版路径的真实鼠标、键盘 mid-action takeover 和 held input 人工检查。Sept21 用户反馈只支持当时动作间接管，不继承为新执行器人工 PASS。
- NOT VERIFIED：长期压力、全新机器及所有硬件/DPI组合、第三方 backend 绕过 UHA 的直连动作。UHA 的方案不能保证其他客户端也遵守。

独立 watchdog/daemon 旧入口现明确拒绝启动，避免第二个 SafetyController 覆盖权限。安全 Banner 保留优先权，UAH 不作为 safety mechanism。详情和准确命令见 PHASE4B_REPORT.md、PERFORMANCE_AND_QUALITY_REPORT.md 与 p4bc-evidence；无条件 P4B UNBLOCKED 仍不成立。

---


日期：2026-09-21。仓库：`E:/UnrealHybridAgent`，本次基线 HEAD `0108a930fd02d0e33f24565bd7475fefba89c94e`，含大量既有未提交改动。

**P4B = BLOCKED。原报告所称自动测试通过属实，但完整安全验收没有完成；本次真实故障注入进一步复现两项 FAIL。**

本报告只用 PASS / FAIL / PARTIAL / NOT VERIFIED / PENDING HUMAN VALIDATION 表示验收结论。PASS 必须结合所在条目的限定范围阅读，任何自动 PASS 都不代表用户物理控制权已获验证。

## 1. 输入与工作区保护

已先执行 git status，保留原有未提交文件；未 reset、clean、提交或覆盖其他会话修改。逐文件写入前核对 SHA-256；本次起点的 diff、状态清单和源文件备份在任务工作区 `work/baseline/`。原报告未修改。

上传文件与仓库副本逐字节 SHA-256 一致：

- P0_1_FIX_REPORT.md：`5c6a1a6e69769197b60f6cb90fc6708d2bb52ceeddca2e54f4346d59672ea463`。
- UAH_PHASE1_REPORT.md：`1477bcbcbf0e976866c5831c13fa7b6194013a5d81b5df967639b1df5218bdc8`。

原十组回归先按修改前代码重跑，exit 全为 0。后续失败与修复后的日志分目录保留，未用最后一次成功覆盖整个历史。一次最早原演示在离线段被真实输入观察器打断，exit=1；该次完整 stdout 仅保留在任务工具记录，后续同名文件保留的是 MCP 502 那次失败，不冒充完整历史。

## 2. 关键修复逐项核验

| 项目 | 结论 | 代码核验及本次处理 |
|---|---|---|
| HUMAN_OVERRIDE / EMERGENCY_STOP 终态锁存 | PASS | `src/safety/controller.py` 的 TERMINAL_STATES 与 release 分支确实存在。本次另修复构造控制器时擦掉持久终态的问题；重启保留终态，损坏旧状态默认锁停。新增回归覆盖。 |
| request_control 不自动重开 | PASS | 终态下 request/grant 拒绝；resume 默认参数改为 false，必须显式传 true，且恢复后仍关闭注入。诊断进程不再假装恢复另一个进程的控制器，须使用运行中的 Safety Banner。 |
| gate fail-closed | PASS | 仅就后续派发：live AND file；文件状态白名单、严格布尔值、过期 lease 拒绝；运行中 observer 不健康或 banner 不可见时拒绝。**不能据此证明已派发动作取消。** |
| injected flag 归因 | PASS | 真实代码按 LLKHF/LLMHF injected flags 处理；有意注入的回调测试明确记 SIMULATED。忽略 injected 并不意味着能证明每个 unflagged 事件必来自用户本人。 |
| hook 专用线程与清理 | PARTIAL | 安装和消息泵确在同一线程，64 位句柄声明存在，真实 hook 安装/停止测试通过；本次要求两个 hook 及线程同时存活。原 stop 未核对 Unhook 返回码，且 production physical callback 可同步触发文件写入/后续 cleanup，不能称完整验证了所有 hook 失败和时限路径。 |
| ledger-driven release | PARTIAL | 不再全量释放 38 项；修复 type_text 异常清空账本、SendInput 零成功仍报成功、清理失败丢账本。本次实测 worker 崩溃且父进程账本仍在时可释放测试键；**Agent/控制器一起崩溃丢失账本，实测 FAIL，见 §4。** |
| forced release 参数 / 响应 | PARTIAL | cleanup 确实使用 args，补充识别 REST 外层 ok 下的 MCP 内层错误；真实强制释放演示通过。`watchdog.py` 虽有 args，但其局部 on_estop 未注册到控制器，响应也未像 cleanup 一样检查；不能把两条路径一起签 PASS。 |
| banner-first | PASS | 原 `uha.py` 仍有 visible OR available，自报可见。已改为实际可见，并注册 banner 活性探针；Win32 probe 验证线程与 IsWindowVisible，失去窗口即拒绝后续输入。真实 Win32 banner 演示通过。 |
| heartbeat | PARTIAL | _gate 刷新 heartbeat、wait 最多 2 秒分块确已实现；原有回归通过。独立 watchdog 没有在 UHA 启动流程中形成完整独立安全闭环，不能宣称 safety-controller crash 已保护。 |
| diagnostics | PASS | 原 collect_safety_status 会创建控制器并改写 gate；已改成只读已有控制器/文件。真实 `uha safety --status` 前后 gate 字节相同；未知 hook/hotkey 显示 unknown/None，user_control_verified=False。所谓不阻塞 OS 的布尔值仍是架构声明，不是全系统驱动审计。 |
| release/急停真实性 | PASS | 修复普通 release 遗留许可字段、远端 release 异常时本地 gate 未关闭、user_control_restored=True 等误导。现在不以软件返回值证明物理控制权；动作中撤销后适配器返回 PermissionError。 |

## 3. 真实后端边界与连接问题

检查了 `E:/MCP/Agent-TARS/server/operator.js`、`server/tools.js`、`server/lock.js` 与实际安装的 `@ui-tars/operator-nut-js/dist/index.js`。mouseSpeed=3600 仍在；tools 在进入动作前检查锁，release 仅释放逻辑锁；没有逐步输入检查或正在执行动作的取消确认。failSafeCorner 配置仍无执行路径。第三方后端源文件未修改。

最初健康 GET 可达而 MCP HTTP 502；仅在测试进程设置 NO_PROXY 后真实演示通过。已在项目自己的 `src/adapters/mcp_client.py` 中对显式 loopback URL 关闭环境代理继承，远端 URL 保持原行为。后续独立真实后端生命周期测试未依赖 NO_PROXY 即可运行。

原演示修正了两处测试风险：离线 gate 断言不再监听真人输入；遇到真实输入不自动 resume，也不在 finally 强行把光标挪回原处。模拟停止后的受控恢复仍允许。所有这些是测试条件修正，不能被解释为放宽生产接管逻辑。

## 4. §22–§31 验收矩阵

实机测试使用新建空白 Canvas、独立随机端口的真实 Agent-TARS（dryRun=false），只杀本次创建并记录 PID 的进程。Canvas 收到真实 Windows 输入：第二次运行记录 2 次 press、2 次 release、37 次拖动事件。F24 的按下/释放通过 GetAsyncKeyState 读 OS 状态；不是只看函数返回。

| 条目 | 验收结论 | 证据与限制 |
|---|---|---|
| §22 物理鼠标接管 | PASS | 22:44 用户本人移动鼠标，观察器记录 mouse_move，HUMAN_OVERRIDE 后后续移动及三次重开均拒绝；用户确认“可以自由移动”。HUMAN VERIFIED，限定动作间接管，不覆盖 mid-move。 |
| §23 物理键盘接管 | PASS | 22:46 用户按 Shift，观察器记录 keyboard，HUMAN_OVERRIDE 后后续移动及三次重开均拒绝；用户确认“按shift可以正常打字”。HUMAN VERIFIED，限定动作间接管；具体 Shift 键由用户确认，日志未记录键码。 |
| §24 CU 正常结束 | PASS | 真实移动完成、release 后 gate 关闭、服务锁为空。仅软件/OS 自动检查，结束后用户能正常操作仍待人工。 |
| §25 worker crash | PARTIAL | 真正杀独立 CU worker，适配器下一次调用失败关 gate；父进程存活且持有账本时，调用 ledger cleanup 后实测 F24 抬起。尚不证明无人调用 cleanup 的独立 watchdog 故障恢复。 |
| §26 agent crash | FAIL | 真正杀 Agent PID 65660，后端继续存活，0.3 秒后 F24 仍为 down。测试随后专门释放该测试键。该清理不能算产品自动恢复。 |
| §27 safety-controller crash | FAIL | 安全控制器与 Agent 同进程，随上述真实进程终止；没有独立存活的输入账本/取消闭环。另有真实控制器进程 kill 后文件 lease 过期测试，但那仅能拒绝未来派发，不能抬起已注入的键。 |
| §28 drag | PARTIAL | 正常真实拖拽结束后左键为 up，Canvas 观测到实际 press/release/motion；动作中撤销仍失败，人工拖动接管未验。 |
| §29 held key | PARTIAL | 真实 F24 down/up 以及 worker crash 后账本清理通过；Agent crash 的 held-key 自动恢复失败。测试未使用文本输入或修改用户文档。 |
| §30 mid-action takeover | FAIL | 真实 backend drag 已派发后模拟撤销，分别约 175.98 / 175.86 ms 才返回；没有后端取消确认。UHA 返回 PermissionError 仅避免假成功，无法撤回期间发生的移动。物理手动 takeover 仍待验。 |
| §31 10× start-finish | PASS | 两次实机运行均完成 10 次真实移动、gate 关闭、锁为空的循环。不能替代人工体验验收。 |

`uah/tests/live_scratch.py` **exit=1 是正确的失败报告**，不是需要把断言改绿的测试问题。另一个不注入真实输入的 blocking-call 故障夹具也返回 exit=1，独立证明适配器不能取消已开始的同步后端动作。

## 5. 后续安全方案与解禁条件

UHA 层已做到：持久终态锁存、拒绝后续输入、保留失败账本、检查真实注入计数、失败响应不能假绿、真实 banner 门禁、诊断只读。**这些不足以解除 P4B。**

下一步必须在实际输入执行边界实现可取消动作：每一小步检查不可被 Agent 重开的一次性 lease/generation，取消后确认 worker 停止；按钮/按键的执行账本须存在于能跨 Agent 崩溃存活的监督层，动作 finally 与 lease 到期均只释放本次实际按下的输入。独立 watchdog 需要单一、明确的撤销权，不能再让两个控制器竞争覆盖同一个 permissive 文件。不能用强制释放逻辑锁替代停止 nut-js 动作。

这些 backend/supervisor 改造本次**未实现、未验证**；没有为了使测试通过而篡改第三方包或关闭人体输入检测。应先在隔离测试桌面完成改造，重新通过 Agent/控制器崩溃、held key、拖拽中的撤销，再由用户验证鼠标/键盘、mid-move、正常结束与崩溃后无需争抢控制权。全部安全 Gate 满足前，P4B 保持 BLOCKED。

## 6. 证据口径

原十组自动回归最终仍为 571/571，新增回归 16/16，不能覆盖上述真实安全失败。真实旧演示为 27/27 自动 + 1/1 模拟，HUMAN VERIFIED=0。其脚本打印 pending=0 仅表示该演示注册的 pending 行为零，**不是整个 §22–§31 人工矩阵完成**。本报告矩阵优先。

失败历史包括 GUI 初次抢焦点、SSE 线程滞留、Tcl 退出挂起、离线测试被真人事件打断和 MCP 502；已修复的最后结果与未修复的安全失败分开列出。完整日志及命令清单在随报告交付的 evidence 目录；仓库另存 `logs/revalidation/20260921/`。


## 实际命令、退出码与日志

工作目录均为 `E:/UnrealHybridAgent`。以下为实际子进程命令；完整 argv、耗时及日志路径也保存在各 JSON 清单。命令中的 `.venv` 指项目 Python 3.13，GUI 使用系统 Python 3.12.10。

- `E:\UnrealHybridAgent\.venv\Scripts\python.exe tests/test_offline.py` — exit=0，248/248，0.96 s；日志 `evidence/validation-final_verified/tests_test_offline.py.log`。
- `E:\UnrealHybridAgent\.venv\Scripts\python.exe uah/tests/test_uah_phase1.py` — exit=0，152/152，21.43 s；日志 `evidence/validation-final_verified/uah_tests_test_uah_phase1.py.log`。
- `E:\UnrealHybridAgent\.venv\Scripts\python.exe tests/test_phase2.py` — exit=0，85/85，1.06 s；日志 `evidence/validation-final_verified/tests_test_phase2.py.log`。
- `E:\UnrealHybridAgent\.venv\Scripts\python.exe tests/test_router.py` — exit=0，25/25，0.14 s；日志 `evidence/validation-final_verified/tests_test_router.py.log`。
- `E:\UnrealHybridAgent\.venv\Scripts\python.exe tests/test_p01_override_survival.py` — exit=0，16/16，0.63 s；日志 `evidence/validation-final_verified/tests_test_p01_override_survival.py.log`。
- `E:\UnrealHybridAgent\.venv\Scripts\python.exe tests/test_phase4.py` — exit=0，13/13，0.47 s；日志 `evidence/validation-final_verified/tests_test_phase4.py.log`。
- `E:\UnrealHybridAgent\.venv\Scripts\python.exe tests/test_phase3.py` — exit=0，11/11，0.25 s；日志 `evidence/validation-final_verified/tests_test_phase3.py.log`。
- `E:\UnrealHybridAgent\.venv\Scripts\python.exe tests/test_p0_safety.py` — exit=0，9/9，0.3 s；日志 `evidence/validation-final_verified/tests_test_p0_safety.py.log`。
- `E:\UnrealHybridAgent\.venv\Scripts\python.exe tests/test_p01_human_override.py` — exit=0，6/6，3.12 s；日志 `evidence/validation-final_verified/tests_test_p01_human_override.py.log`。
- `E:\UnrealHybridAgent\.venv\Scripts\python.exe tests/test_phase4b.py` — exit=0，6/6，0.44 s；日志 `evidence/validation-final_verified/tests_test_phase4b.py.log`。

附加验收：

- `E:\UnrealHybridAgent\.venv\Scripts\python.exe uah/tests/test_phase11.py` — exit=0，6.72 s；日志 `evidence/checks/phase11_verified.log`。
- `C:\Users\PUBLIC_USER\AppData\Local\Programs\Python\Python312\python.exe uah/tests/gui_smoke.py` — exit=0，6.0 s；日志 `evidence/checks/gui_dashboard.log`。
- `C:\Users\PUBLIC_USER\AppData\Local\Programs\Python\Python312\python.exe uah/tests/compact_smoke.py` — exit=0，11.24 s；日志 `evidence/checks/compact_final_verified.log`。
- `E:\UnrealHybridAgent\.venv\Scripts\python.exe -m uah.tools.uah selftest` — exit=0，0.55 s；日志 `evidence/checks/selftest.log`。
- `E:\UnrealHybridAgent\.venv\Scripts\python.exe uah/tests/perf_probe.py` — exit=0，13.61 s；日志 `evidence/checks/performance.log`。
- `E:\UnrealHybridAgent\.venv\Scripts\python.exe uha.py demo1 --actor Hall_Floor --delta 20 --dry-run` — exit=0，8.15 s；日志 `evidence/checks/cli_progress.log`。
- `E:\UnrealHybridAgent\.venv\Scripts\python.exe <PRIVATE_EVIDENCE_PATH>` — exit=0，1.19 s；日志 `evidence/checks/diagnostics.log`。
- `NO_PROXY=127.0.0.1,localhost E:\UnrealHybridAgent\.venv\Scripts\python.exe -m tools.p01_override_demo` — exit=0，2.84 s；日志 `evidence/checks/live_no_proxy.log`。
- `C:\Users\PUBLIC_USER\AppData\Local\Programs\Python\Python312\python.exe uah/tests/live_scratch.py` — exit=1，13.77 s；日志 `evidence/checks/live_scratch_crash.log`。
- `E:\UnrealHybridAgent\.venv\Scripts\python.exe tools/p01_lifecycle_revalidation.py` — exit=1，2.71 s；日志 `evidence/checks/lifecycle_verified.log`。

语法：`E:/UnrealHybridAgent/.venv/Scripts/python.exe <PRIVATE_EVIDENCE_PATH>`，exit=0；逐文件调用 `py_compile.compile(..., doraise=True)`，127 个仓库 Python 文件，**syntax failures=0**。范围为所有 Git 跟踪及未忽略的新 Python 文件；不编译外部 `.venv` 依赖。清单 `evidence/compile.json`。`git diff --check` exit=0。


## 人工验收补记（2026-09-21 22:44–22:47）

本节更新此前“新版本尚无人工鼠标/键盘验证”的时间点结论。两轮均由用户点击开始；倒计时后连接独立真实 Agent-TARS、显示 Safety Banner，在空白测试区域完成一次鼠标移动，然后等待用户实际输入。没有模拟 physical callback，没有自动恢复真人触发的 HUMAN_OVERRIDE，没有接管后光标复位。

- 鼠标：用户确认“可以自由移动”；真实 mouse_move 触发终态，后续 move 拒绝、三次 request_control 拒绝。PASS / HUMAN VERIFIED，范围仅动作间接管。
- 键盘：用户确认“按shift可以正常打字”；真实 keyboard 触发终态，后续 move 拒绝、三次 request_control 拒绝。PASS / HUMAN VERIFIED，范围仅动作间接管。未单独询问 banner 文案是否被用户看到，不把此项当作视觉验收。
- 每轮 cleanup.json 均确认清理完成；独立后端被测试程序主动终止，记录 exit=1，不是 backend 正常结束用例的 PASS。未终止用户原有后端。
- 原始 status.json 的 PENDING USER CONFIRMATION 保留作为当时记录，后续用户陈述与结论另存 user_confirmation.json，不改写原始事件。

证据：`human-validation/20260921-224439-mouse/` 与 `human-validation/20260921-224651-keyboard/`；仓库归档在 `logs/revalidation/20260921/human-validation/`。

**P4B 仍 BLOCKED。§26/§27 崩溃后按键未回收与 §30 动作中撤销 FAIL 均未改变；drag、held-key、正常结束/崩溃后的人工操作以及动作中途真实接管不能由本轮外推通过。**
