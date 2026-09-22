# Phase 4C Gateway 与生命周期

## 2026-09-22 最终安全收尾（本节为最新结论）

本机受支持路径 P0 验收 PASS；P4B 安全阻塞已解除（UNBLOCKED）。本机 v0.1 为 READY WITH KNOWN ISSUES；公开发布未批准、历史秘密审计未完成。已完成三项真人适配器链路验收，后续 hook/资源清理增强通过 76 项真实 OS 故障与清理检查、完整适配器自动回归及十组全回归。140 Python 文件 syntax failures=0。

完整修复、失败保留、准确命令、有限延迟及未验证边界见 P0_1_FINAL_VALIDATION_REPORT.md。以下 BLOCKED/NOT READY 是此前阶段历史结论，不再代表本节定义的本机受支持范围。


2026-09-22。结论 CONDITIONAL PASS，完整产品发布 NOT READY。

## 实现

新 ProviderGateway 在现有 adapters 上统一装配、健康、能力、初始化、执行审计、取消、重连、关闭；不另建 Router。BackendBundle 将结构化方法通过 Provider facade 进入现有运行时。装配错误保留真实原因，能力缓存保存错误并在失败时失效。

健康区分 healthy/degraded/disconnected/misconfigured/unavailable。Commandlet 明确是离线进程，不能声称拥有当前未保存场景。`python uha.py providers` 是可执行诊断入口。

本机最后诊断：Unreal MCP 与 UE Python healthy，启发式视觉可用；共享端口 8788 Computer Use disconnected，Commandlet disconnected。GUI 专项使用了测试自己启动的随机端口服务，结束后关闭，没有将默认端口伪装在线。

MCP stdio 用独立 reader+queue 实现响应时限，解决 readline 永久阻塞。真实沉默子进程测试确认超时并退出。死进程不能继续复用缓存“已连接”；握手失败释放坏 client，显式重连建新会话。

取消首先拒绝后续派发；结构化远端在途调用不能由现有协议强制撤销，返回 remote_execution_stopped=false。不能用 cancel() 返回值宣称 UE 已停。执行中不允许重连，写入失败不在 Gateway 盲重试。Router 继续使用原有方法匹配、风险、预算、健康及非幂等保护，不冒称新增了通用跨 Provider 自动重放。

DESKTOP 在 registry 中诊断、能力和生命周期可见；现有 CU adapter 仍作为执行入口以保留安全契约，不硬套结构化接口。VISION_HEURISTIC 使用同一注册描述与实际 callable，不引入云模型或密钥。

## 验证与边界

真实杀掉测试自有 MCP 进程后，运行时通过 UE_PYTHON 查询同一 Actor，数据一致，显式重连后 MCP healthy。见 disconnect-20260922-084212/result.json。这是实际进程故障，不是 mock.available=false。

16 项专项覆盖取消后禁止 dispatch、在途禁止 reconnect、不确定写不重试、配置错误、能力缓存错误、静默真实子进程超时。完整原十组回归退出码全部 0。审计区分 provider_response 与独立 verify，不包含整个调用参数或屏幕二进制。

未覆盖：远端进程树无限挂死、同时多个故障、全新机器安装、远端网络分区与 UE 重启后自动恢复正在执行的变更。Token/任意脚本隔离不是本地 Gateway 提供的安全边界。完整杀进程树支持需要单独隔离设计，不能贸然终止共享服务。

UAH 为 ACTIVE FIRST-CLASS，未拆仓、未改成日志替代品；复用正式 progress hook、Protocol/Status/Render 和独立 Hub。ControlOverlay 保留旧稳定职责，不为了外观一致重写安全 banner。
