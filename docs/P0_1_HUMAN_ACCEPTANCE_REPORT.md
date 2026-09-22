# 真人安全验收记录 — 2026-09-22

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


结论：以下三项均为 **PASS（真人参与，限定于本机实际测试路径）**。

1. 鼠标接管拖拽：真实、无 injected 标志的 mouse_move 发生在进行中的 drag 调用内；worker 返回 physical:mouse_move 并终止动作。用户确认鼠标正常。
2. 按键接管拖拽：真实 keyboard 事件发生在 drag 调用内；worker 返回 physical:keyboard 并终止动作。用户确认停止且正常操作。
3. 按住 Shift 时接管：OS 查询先确认 Shift 确实处于按下状态；用户真实键盘事件触发接管，随后 OS 查询 Shift 已释放，用户确认打字和操作正常。

三项均进入 HUMAN_OVERRIDE，allow_input=false，后续输入拒绝，连续三次 request_control 自动重开均拒绝。左键和 Shift 最终均释放，worker cleanup.remaining=[]，worker 退出码均为 0。原始日志和守护进程清理记录已复制保存。

## 证据范围

执行路径为生产 GuardedInputClient → 独立 SendInput worker / guardian，配合真实 SafetyController、Win32 Safety Banner 与物理键鼠低层 hook。本轮没有合成接管回调，也没有把 injected 输入当人工。用户在测试窗口选择“正常：停止且可以正常操作”，并在对话确认各项完成。

本次没有经过 ComputerUseAdapter 的远端租约/网络调用链，也没有在 UE 中进行真实物理接管。因此三项 PASS 不能自动推出完整 Computer Use 链路 HUMAN VERIFIED 或 P4B UNBLOCKED。此前真实 GUI 保存链路的自动 PASS 保留，但与本次分段人工验证不能合并伪装成一次完整端到端人工验收。

尚待：适配器完整链路的真人接管；此前报告中的 hook 超时/unhook 返回诊断、双故障与压力边界仍未消除。P4B 安全放行保持 BLOCKED，v0.1 保持 NOT READY。原报告中“新版 worker 的三项人工验证 pending”在本报告限定范围内已解除，其他 pending 未自动解除。

## 验收工具修复与历史失败

首次测试窗口的三个尝试均出现 Safety Banner 未显示，未成功开始输入。其中一条用户点击正常反馈后被旧工具错误降级为 PARTIAL；依据实际 error，这三条准备失败均按 FAIL 处理，不算人工接管通过。

原因是含 tkinter 的 Python 中，后台验收线程选择了 Tk banner，导致跨线程 Tcl 问题。验收工具改用已有 Win32 banner；独立预检确认 banner started=true、visible=true、input_allowed=false，停止后 closed=true。随后新会话三项通过。修复只针对验收工具，不宣称生产 Tk banner 的所有线程场景已经修复。

证据目录：human-validation-20260922；原始成功会话 logs/runs/human-acceptance-20260922-122045。日志内 user_control_verified=false 是控制器自动快照原值；人工结论另由物理事件、OS 释放结果及用户确认组合形成，未篡改快照。

本次不据动作调用结束时间声称精准输入停止延迟，未测量每次 OS 注入与物理事件之间的完整时序。
