# P4B/P4C 实施前审计（2026-09-22）

本轮基线保留当前全部未提交改动，未 reset、clean、commit 或 push。原始 status、diff 和 153 个源文件备份已保存到本次任务的 `work/p4bc/baseline`；报告和证据的公开副本不得包含本地凭据。

## 模块地图及分类

- ACTIVE：`src/planner` / `src/router` / `src/scheduler`，显式计划、成本与规则路由、执行与验证、有限降级。
- ACTIVE：`src/adapters/unreal`，MCP、Remote Python、Commandlet；保留 Phase 4A，禁止为统一命名重写。
- ACTIVE / FIRST-CLASS：`uah/`，Protocol/Core/HTTP+SSE/Adapter/Compact Desktop；可选独立 HUD，同仓库不拆分。
- ACTIVE：`src/core/checkpoint.py`、session_control、session_locks、log，检查点、审批、任务历史。
- P4B_REQUIRED：`src/observation`、`src/semantics`、`src/verification`、batch_ops、batch_mutation；已存在实现，不能声称从零新建。
- P4C_REQUIRED：`src/runtime.py` 和 capability_cache；目前生命周期、能力和健康信息过粗，装配失败被吞掉。
- P4B_REQUIRED / 安全阻塞：Computer Use adapter、safety/controller、human_override、watchdog。普通鼠标/键盘动作间接管人工通过，但旧后端 mid-action 和进程崩溃仍失败。
- ACTIVE：vision 几何、ground/support、GUI confidence；需真实场景验证，不能将规则命中视为确定性物理证据。
- LEGACY_BUT_REFERENCED：ControlOverlay 与原 Dashboard，保留兼容，不整体删除。
- UNKNOWN：单独 watchdog 入口仍有使用说明，但与运行期未构成独立回收闭环，先修复/限定，不凭静态无引用删除。
- GENERATED / CACHE：`.state/`、logs、截图、编译缓存、临时探针输出。`.gitignore` 对已跟踪文件不生效，需公开候选文件清单审计。
- DEPRECATED 候选：README 旧限制和早期数量；保留历史阶段报告并增加当前说明，不篡改历史证据。
- tests/tools/docs/config：分别保留正式回归、诊断入口、阶段证据、本地配置与示例配置边界。

## 基线结果

原十组测试实际全部 exit=0（原计数共 571 项）；命令、时长及原始日志在 `validation-p4bc_baseline/results.json`。没有将离线测试升级为 UE PASS。

实际 `uha.py doctor` exit=2：UE 未运行、12029 未监听、MCP 实际读取失败、Computer Use 不可达。旧 plugin 日志只能证明历史启动，不代表在线。GUI focus_point 为负坐标，必须在当前桌面重新验证，拒绝沿用。

仓库已有的 Commandlet `available=True` 仅说明可执行文件配置可用，不能等价于当前编辑器场景可读写。后续需对 method capability 做区分。

## 开始修改前已识别的缺口

1. TaskObserver 每次拉取全场景，即使只请求少数 Actor；delta 比较发生在 apply 后，可能丢失前态；批量读取不存在对象仍可能被当作事实。
2. batch_read 在每个 label 内重新遍历世界，O(N×M)；元数据不足，语义层依赖另一趟全量详情。
3. rollback 只恢复 location，虽保存 rotation/scale 却不恢复；跨关卡身份检查不足。
4. Provider 未统一可诊断状态；异常装配缺乏原因；capability cache 丢弃具体错误。
5. 旧 Computer Use 后端不能取消在途动作，输入账本随 Agent 崩溃消失。须在输入执行边界修复，不能只改报告。

P4B 安全 Gate 在新证据形成前继续 BLOCKED；可独立推进结构化通道与离线开发，不把 GUI 安全问题当作阻止所有代码工作的理由。
