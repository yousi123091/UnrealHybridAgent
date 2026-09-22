# 性能与质量报告 — 2026-09-22

## 2026-09-22 最终安全收尾（本节为最新结论）

本机受支持路径 P0 验收 PASS；P4B 安全阻塞已解除（UNBLOCKED）。本机 v0.1 为 READY WITH KNOWN ISSUES；公开发布未批准、历史秘密审计未完成。已完成三项真人适配器链路验收，后续 hook/资源清理增强通过 76 项真实 OS 故障与清理检查、完整适配器自动回归及十组全回归。140 Python 文件 syntax failures=0。

完整修复、失败保留、准确命令、有限延迟及未验证边界见 P0_1_FINAL_VALIDATION_REPORT.md。以下 BLOCKED/NOT READY 是此前阶段历史结论，不再代表本节定义的本机受支持范围。


结论：受控范围内自动验收 PASS；整体发布状态 NOT READY。所有测试在本机实际执行；PASS 不等价于物理人工接管已完成。

## 自动测试与命令证据

十组最终回归全部退出码 0，结果见 `p4bc-evidence/validation-p4bc_release/results.json`，每条包含完整解释器、参数、时长和原日志路径。各脚本独立执行；UAH Phase 1 内部还调用旧回归，不将嵌套数量重复累计。

脚本：test_offline、test_uah_phase1、test_phase2、test_router、test_p01_override_survival、test_phase4、test_phase3、test_p0_safety、test_p01_human_override、test_phase4b。原十组外部汇总计数 571 项，包含嵌套重复，不能解释成 571 个独立用例。

- `tests/test_p4bc.py`：16 项 PASS，退出码 0。包含真实沉默子进程的超时及退出，不只是函数返回值。
- `uah/tests/test_phase11.py`：16 项 PASS，退出码 0；批准/拒绝、超时、重复 req_id、断线、权限和 Hub 生存期。
- `uah/tests/compact_smoke.py`：实际创建 320×48 窗口；无标题栏、topmost、不激活、拖动、三态、多 Agent、持久化、提醒、Hub 重启、销毁 PASS；进程退出码 0。
- `uah/tests/gui_smoke.py`：旧 Dashboard 实际窗口 19 项 PASS；退出码 0。
- `python -m uah.tools.uah selftest`：真实 HTTP/SSE 链路 11/11 PASS；退出码 0。
- `uah/tests/guarded_input_smoke.py`：真实空白测试画布；10 个完整输入会话、三个实际进程终止、故意冻结许可文件后的动作撤销 PASS。接管触发为模拟，不是人工。
- 全仓库 py_compile：136 个 tracked/nonignored Python 文件，syntax failures=0。排除第三方虚拟环境；结果 `compile-result.json`。
- `git diff --check`：退出码 0；仅存在 Windows 行尾转换提示。

所有附加测试的准确命令/退出码在 `p4bc-evidence/checks/*.json`，日志同名 `.log`。GUI 使用含 tkinter 的 Python 3.12.10；运行时用项目 Python 3.13。新机器安装验收 NOT VERIFIED。

## 真实 UE 与性能

测试使用 `/Game/UHAValidation/` 专用关卡，36 个立方体与显式地面。最后运行 `p4bc-20260922-084837`，读取 12 个 Actor：逐项 323.48 ms、12 次往返；批量 34.13 ms、1 次往返，往返减少 91.67%。这是单次本机测量，无 p95、无多机性能承诺。

TaskObserver 指定对象不再额外全世界发现；UE 批量脚本仍需一次世界枚举建立索引，因此并非引擎原生 O(1) 查询。公开完整场景/请求邻居时才做更广发现。同 scope 新一轮事实替换旧事实，delta 基于变更前快照。

10 类验收以主矩阵加 GUI 与断连专项组合。每类真实路径写在 PHASE4B_REPORT.md，不能把夹具布置算为 Planner 完成。Provider 审计记录 160 事件。

## 必须保留的失败

1. 第一次 GUI fallback 实际保存成功，但报告调用 as_dict(full=True) 报 TypeError；修正后重跑。
2. 第二次调用 UE new_level 在 TickTaskManager.cpp:1992 触发真实引擎断言。已禁止实时脚本 new/load/open world；改为启动指定专用关卡。限制不是引擎修复。
3. 并行 GUI 测试前台变化导致一次 Compact 焦点断言失败，隔离重跑通过。
4. 一次 mid-drag 模拟撤销没有打断动作，测试 FAIL。不能证明仅是并行影响。新增独立取消信号，额外注入“许可文件保持 ALLOWED”故障，真实 OS 输入停止并释放；物理接管与长期压力仍 pending。
5. 中间回归旧 FakeBackend 缺 current_level，新增跨关卡保护导致测试失败；补充测试后端当前关卡能力后完整重跑，不删除生产检查。

历史失败日志不删除、不覆盖为 PASS。libpng tRNS 警告仍在 GUI 日志，未影响已观察到的输入和窗口行为。
