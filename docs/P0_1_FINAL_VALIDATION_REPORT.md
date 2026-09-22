# P0 安全收尾验证 — 2026-09-22

## 最新结论

**本机受支持路径的 P0 验收 PASS，P4B 安全阻塞解除（UNBLOCKED）。本机 v0.1：READY WITH KNOWN ISSUES。**

该结论限当前 Windows、UHA 守护输入路径、现有结构化能力及已测试操作；不是全平台或无限故障安全认证，也不是公开发布批准。源码可继续本机使用和 P4B/P4C 工作，无需重复已经完成的三项人工操作。

放行依据是此前真实 UE 任务/独立验证/回滚与降级、用户已完成的完整适配器链路三项真人接管、以及本轮故障修复后的真实 OS 和回归结果共同构成。没有仅依据 release() 成功或单份报告判断。

## 本轮实际修复

1. Native hook 清理核对 UnhookWindowsHookEx 的 BOOL 与 Win32 错误码，保存真实结果；失败时保留诊断，不再无条件记成 stopped。检查消息泵心跳，卡住不再等价于线程健康。
2. 发现原 hook 可能已被移除但句柄变量/线程仍存在。增加 200 ms 周期的原生更新检查：先注册新观察器，再核对旧 hook 的卸载；旧 hook 丢失即锁存 unhealthy，worker 停止并释放，绝不因此自动恢复注入。没有为健康检查额外合成按键。
3. 输入 worker 和 guardian 同时退出时，仍存活的 owner 在确认 worker 已退出后用写前账本兜底释放。没有在 worker 仍可继续写入时竞态释放。
4. 压力测试曾在第 17 次附近触发真实进程崩溃。Win32 banner 原固定窗口类保留旧回调指针；改为实例独立类名，在所属线程销毁窗口并注销类，保留清理结果。Windows 默认使用 Win32 banner，避免含 tkinter 的解释器从后台线程创建第二个 Tcl UI。
5. 输入进程初始化等待异常会主动关闭子进程；banner start 等实际可见才成功返回。

## 最终验证

`tools/p4bc_final_safety.py` 最终退出码 0，**76/76 检查 PASS**，约 29.46 秒：

- 1 项真实 native unhook 及卸载后没有 callback。
- 30 次完整启动、真实 F24 down/up、worker 正常退出和 hook 清理。
- 30 次 banner 原生窗口类注销、线程退出。
- 6 种许可故障：文件缺失、损坏、租约过期、心跳失效、banner 不可用、共享 gate 保持允许但直接撤销。均 OS 查询确认按键释放、后续输入拒绝。
- 3 个单进程终止：owner、worker、guardian；3 组双进程终止：owner+worker、owner+guardian、guardian+worker。均真实终止自有进程且查询 OS 按键状态。本轮约 11–22 ms 后释放。owner 与 controller 同进程，未冒充独立 controller 服务崩溃测试。
- 3 个 hook 故障：实际卸载后释放约 6.5 ms；单 worker 消息泵卡住约 506 ms；通过原生 API 移除 hook 但保留旧 Python 变量约 168 ms 后发现并释放。

故障时中间快照可能是 cleanup_failed / cleanup_complete=false，这是如实记录卡住线程或“hook 已不存在”的错误，不能当成卸载函数成功。相关 PASS 验证的是拒绝输入和真实按键释放；正常 30 次会话另行要求完整清理成功。

补强后 `tools/p4bc_adapter_closeout.py` 经真实 HTTP 服务与公开 ComputerUseAdapter，再次验证租约获取、持有真实 F24 跨 7 次 hook 更新、模拟撤销、OS 释放、终态锁存、status 读回 holder=null，退出码 0。此项明确是自动模拟撤销，不代替人工记录。

`uah/tests/guarded_input_smoke.py` 实际空白画布中拖拽与撤销通过；按钮 down/up 均被画布接收，停止后光标保持不动。另有 10 个完整会话和实际进程死亡回归。未修改第三方 Agent-TARS。

最后完整十组原回归退出码全部 0；Phase1.1 16 项、P4BC 专项 16 项通过。全仓库 **140 个 Python 文件，syntax failures=0**；git diff --check 退出码 0。检查结束没有本轮测试/guarded input 残留进程。

## 真人证据与版本边界

用户已完成 worker 边界及完整公开适配器链路的鼠标 mid-drag、键盘 mid-drag、held Shift 三项，并明确反馈“均正常”。物理事件、OS 释放结果、后续拒绝、三次禁止重开、服务 status 租约释放均一致。证据见 P0_1_HUMAN_ACCEPTANCE_REPORT.md 与 human-validation-20260922/full-adapter-chain。

真人测试早于本轮 hook 健康和 banner 清理增强；其原始版本证据保持原样。本轮增强使用真实 OS 自动故障与完整适配器回归验证，未伪称用户亲测过新增故障。没有因此把原人工 PASS 擦掉，也没有把自动故障测试改成人工 PASS。

## 失败记录保留

首轮原生横幅压力崩溃退出码 3221226525；第二轮 silent_unhook 保持按键 down，明确 FAIL；修复后重跑通过。一次测试被实际 physical:mouse_move 中断，保留为未完成运行，之后空闲输入下重跑，未禁用真人接管保护。早期同时阻塞两个 detector 消息泵测得约 1.5 s，保留证据；最终单 worker 消息泵故障约 506 ms，两种不同故障不混淆。

## 已知限制（不扩大 PASS）

有限步骤停止仍存在 OS 调度及检查/注入窗口；上述数字是单机样本，不是最坏情况或零延迟保证。长时间系统冻结、三个保护进程同时死亡、操作系统/驱动故障、磁盘和进程组合灾难、其他客户端绕过 UHA 输入不在本次可靠性声明内。NOT VERIFIED：多小时 soak、全新机器、多硬件/DPI、OS 内部真实超时移除的所有触发条件；本次确实覆盖其“hook 已失效但旧变量仍在”的故障状态。范围内故障门槛已关闭，不将无限故障容忍作为后续移动的验收目标。

已有产品边界不变：复杂 mesh 支撑仍可能 UNKNOWN；结构化远端在途执行不保证可强制取消；UE tick 中 new/load world 保持拒绝；公开仓库历史秘密审计未完成，不能直接公开发布。这些写入已知问题，不伪装成已实现能力。

## 证据与冻结

准确命令、退出码、时长在 final-safety-evidence/checks/*.json 和 validation-safety_frozen/results.json；失败、成功日志都保留。最终源码哈希在 source-manifest.json（含路径相对名、SHA256），仅表示本次审阅快照，没有擅自提交、打 tag、push 或创建远程仓库。
