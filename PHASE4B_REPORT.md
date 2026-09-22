# Phase 4B 实施与验收

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


2026-09-22。功能结论 CONDITIONAL PASS；安全 Gate BLOCKED；不是正式放行。

## 已实际实现

保留 Planner/Router/Executor、UAH、MCP/Python 后端。TaskObserver 支持限定对象的批量事实和真实代次/delta，不再将缺失对象变成原点事实或留存上次对象。批量 UE 代码一次建立世界索引，返回 path/class/level/folder/tags/visibility/parent/bounds。标签重名拒绝写入，三维参数必须完整且有限。

Batch mutation 写前捕获完整 transform；缺失对象拒绝写入。回滚先核对关卡，恢复 location/rotation/scale 后读回验证。语义标签仍依赖可观察元数据，不把标签推断当真实碰撞。

OutcomeVerifier 独立检查动作语义及显式实体平面支撑；无几何模型或缺 bounds 返回 UNKNOWN。复杂网格、斜面、悬空结构不能通过 AABB 被宣称落地。不同接口的返回 success 从来不是最终验证证据。

GUI 保存只允许当前实际 UE 窗口、范围内坐标；原缓存负坐标被拒绝。输入在 UHA 自己的独立 SendInput worker 执行，每步检查许可及前台窗口；第三方 Agent-TARS 只提供共享锁/截图。本轮未修改第三方后端。

## 真实测试矩阵

1. PASS：自然语言 Planner → Executor → Router → Unreal MCP 位移并保存；独立读回 Z=70。
2. PASS：真实批写两个存在 Actor 和一个缺失 Actor，实际部分失败；独立验证指出失败对象，没有整体假绿。
3. PASS：明确目标的 scoped observation、外部改变后的 delta、36 个标签筛选；基础语义 profile 接口沿用原架构，不等于任意自然语言理解。
4. PASS（明确实体平面夹具）：贴地、浮空、边界间隙，修复后生产 OutcomeVerifier 读回 PASS。复杂支撑 PARTIAL。
5. PASS（专项）：注入“结构化保存派发前拒绝” → 真正 KEYBOARD Ctrl+S；磁盘 mtime 增长、dirty 从 1 到 0、位置读回，4/4 保存检查。不是用结构化保存冒充 GUI。证据 gui-fallback-20260922-083942。
6. PASS：故意设错预期位置，独立 verifier FAIL，随后真实回滚并读回。
7. PASS（专项）：终止本次拥有的 MCP stdio 进程，healthy→disconnected；运行时切到真实 UE_PYTHON 读取，重新连接后 MCP 恢复。未重复写入不确定动作。证据 disconnect-20260922-084212。
8. PASS：完整 transform 被改变后真实恢复，并检查旋转/缩放/位置。
9. PASS：路由、执行、响应、验证、恢复审计可追踪；主运行 160 事件。
10. PASS：真实五步 find/inspect/move/inspect/save 运行时任务，最终 Z=60，磁盘保存。

主矩阵脚本单独把第 5 项写 NOT VERIFIED，因为其不调用 GUI；以上第 5 项由独立专项补足，未修改原始矩阵输出伪造 PASS。主矩阵见 p4bc-20260922-084837；第一次基线主矩阵也保留。

## 安全和兼容限制

P4B 不得 UNBLOCKED：新版动作中真实鼠标/键盘 takeover 尚未由用户验证，软件检查与 SendInput 之间仍有调度窗口，不能承诺 human wins immediately 的零延迟。旧 backend bypass UHA 的直连调用不受这套守护保护。

UE 实时回调中替换 world 已真实造成崩溃；入口拒绝 new_level/load_level/open_level 等常见 API。AST 规则是受支持 API 限制，不是任意 Python 的安全沙箱，动态反射不保证拦截。测试关卡需在编辑器启动时指定。

本次不扩大为一般场景生成或建筑合理性智能体。复杂网格、跨关卡回滚迁移、全场景多步语义推理仍在 v0.2 路线。代码、可复查命令、真实失败均已交付。
