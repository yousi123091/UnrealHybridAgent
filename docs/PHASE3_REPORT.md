> Public edition: project names are anonymized; historical measurements are retained.

# Phase 3 Acceptance Report — UnrealHybridAgent

**Machine**: Windows 11 · UE 5.8.2 · Project `ExamplePalace` · Date 2026-09-20  
**Runtime for real demos**: system Python 3.12.10（含 tkinter 8.6 / httpx / pywin32）  
**Branch**: `phase3` · Workspace override: in-place at `E:\UnrealHybridAgent`（用户确认；Git 已安装并初始化）

---

## Phase 3 Acceptance Matrix

| Capability | Code | Offline Test | Real UE Test | Result |
|---|---|---|---|---|
| MCP Actor Read | ✅ | ✅ | Demo A / suite `actor_find`+`actor_inspect` 回读成功 | **PASS** |
| MCP Actor Mutation | ✅ | ✅ | Demo A `Platform_Skirt_Front` Z 650→655，独立回读一致 | **PASS** |
| Verify / Readback | ✅ | ✅ | 三次独立 `get_actor_transform` 与 Executor verify 一致 | **PASS** |
| Save | ✅ | ✅ | `level_save`：KEYBOARD 首选失败（mtime 未变）→ fallback UNREAL_MCP 成功（mtime 推进） | **PASS** |
| Semantic Resolver | ✅ | ✅ | 真实 8 个 Actor 分类：Roof/Beam=STRUCTURE 不误判 GROUND；UNKNOWN→vision | **PASS** |
| Precise Ground Place | ✅ | ✅ | `line_trace` 命中 `EXT_Terrace_Course1` Z=420（绝对支撑面可算）；完整 auto-place 未在本轮批量执行 | **PARTIAL** |
| Vision fallback | ✅ | ✅ | VLM 未配置（`vlmConfigured=false`）→ 明确 skipped，不假绿；viewport 截图可用 | **PARTIAL** |
| GUI Calibration | ✅ | ✅ | 窗口变化后自动重标定；ROI/focus_point 更新 | **PASS** |
| GUI Interaction | ✅ | ✅ | DesktopLock + CU click focus [810,437] + release | **PASS** |
| Pause | ✅ | ✅ | 真机：PAUSED 时写被拒；`end_task` 不再冲掉 PAUSE | **PASS** |
| Resume | ✅ | ✅ | Resume 后 mutation 成功（650→652） | **PASS** |
| Stop | ✅ | ✅ | STOP 后 mutation 被拒；`end_task` 不清除 STOP | **PASS** |
| Emergency Stop | ✅ | ✅ | **钩子真机释放**：keys/buttons、CU `release_control`（holder→null）、coordinator `force_release_all`、DesktopLock；phase=ABORTED 且经 `end_task` 仍保持；后续 mutation 被拒 | **PASS** |
| DesktopLock | ✅ | ✅ | acquire→CU action→release；health idle | **PASS** |
| UE Write Lock | ✅ | ✅ | mutation 走 write 锁；offline 测 max writers=1 | **PASS** |
| Router fallback | ✅ | ✅ | `level_save` KEYBOARD fail → UNREAL_MCP ok（mtime 证据） | **PASS** |
| Quality Ledger | ✅ | ✅ | `.state/router_quality.json` 已有真实任务样本（见下） | **PASS** |

状态仅使用：PASS / PARTIAL / FAIL / NOT TESTED。

---

## 1. 本轮实际修复了什么

### 1.1 UnrealMCPython TCP 12029 不可用

| 项 | 内容 |
|---|---|
| **问题** | UE 进程在，但 12029 未监听；`available()=False` |
| **根因** | 当前 Editor 会话只 `Mounting` 插件，**未** `InternalLoadLibrary` DLL，故 `LogMCPython: TCP server started` 未出现。历史日志证明同一插件二进制在 08:23 会话曾成功启动 TCP。属于**会话/模块加载失败**，非插件永远坏掉。 |
| **修复** | 用户授权后优雅停止 Editor（PID 50896）→ 用 `.uproject` 正确重启 → 插件 DLL 加载 + TCP 监听 |
| **验证** | `tools/phase3_doctor.py` 分层探针：module_loaded=true，tcp_listening=true，`level_probe=/Game/Example/ExamplePalace` |
| **结果** | **PASS**（真机请求成功） |

### 1.2 doctor 假绿风险

| 项 | 内容 |
|---|---|
| **问题** | 原 doctor 主要打印通道 available，难以区分「进程在 / 插件在 / TCP 在 / 请求成功」 |
| **根因** | 缺少分层真机探针 |
| **修复** | 新增 `tools/phase3_doctor.py`；`uha.py doctor` 改为调用分层报告；仅 `mcp_request_ok=true` 才 exit 0 |
| **验证** | 真机输出 8 层信号 + gates |
| **结果** | **PASS** |

### 1.3 Pause / Stop / EStop 不持久（真机 + 评审发现）

| 项 | 内容 |
|---|---|
| **问题 1** | `pause()` 后 `executor.run(actor_move)` 仍成功改 UE |
| **根因 1** | `SessionController.begin_task()` 无条件重置 `command/phase`，`Executor.run()` 开头调用它 → 暂停被冲掉 |
| **问题 2（评审 critical）** | `end_task()` 同样无条件 `command=NONE` + `DONE/FAILED`，中止/暂停在任务收尾时被洗掉 |
| **问题 3（评审 critical）** | 控制面脚本未挂 `on_emergency` 钩子，锁是 harness 自己放的，不能证明 EStop 生产路径 |
| **修复** | `begin_task`/`end_task` 在 PAUSE/STOP/ESTOP/ABORTED 下**不清理**控制命令；新增 `acknowledge_control()` 供测试显式复位；`Executor.run` 对 write-like skill 在 `blocks_writes()` 时直接失败；`wait_if_paused` 超时不再假放行；`phase3_control.py` 按生产方式注册紧急钩子 |
| **验证** | `tests/test_phase3.py` **11/11**；真机 `tools/phase3_control.py`：hook_log 显示 keys/CU/coordinator 均由钩子释放；`desktop_released_by_hooks=true`；`estop_survives_end_task=true` |
| **结果** | **PASS**（修复并复验后） |

### 1.4 Overlay / Python 运行时

| 项 | 内容 |
|---|---|
| **问题** | `.venv` Python 3.13 无 tkinter，Overlay 不显示 |
| **根因** | 精简/嵌入式 Python 常不含 Tcl/Tk |
| **修复** | 用户选择：真机验收用系统 Python 3.12；安装 httpx + pywin32（清华源）；Overlay `tkinter_available=true` |
| **验证** | doctor overlay 层；Demo C `overlay.available=true` |
| **结果** | **PASS**（显示能力）；测试进程里 overlay 线程偶发 `Tcl_AsyncDelete`（不影响主流程，记 minor） |

### 1.5 Git 工作区

| 项 | 内容 |
|---|---|
| **问题** | 项目无 Git，PATH 无 git |
| **根因** | 本机未安装 |
| **修复** | npmmirror 下载 Git 2.46.2 并安装；`E:\UnrealHybridAgent` 初始化；分支 `phase3` |
| **验证** | `git log` 可用 |
| **结果** | **PASS**（就地工作覆盖已记录） |

---

## 2. Demo A 完整证据

**目标 Actor**: `Platform_Skirt_Front`（场景真实存在；非假定 Hall_Floor）  
**Level**: `/Game/Example/ExamplePalace.ExamplePalace`

### Before
- Name/Label: `Platform_Skirt_Front`
- Location: `[4850.0, -3700.0, 650.0]`
- Scale: `[1.8, 30.0, 4.6]`
- Timestamp: 2026-09-20T21:21:50

### Execution
- Router first choice: **UNREAL_MCP**（reason：需要精确数值）
- Method used: UNREAL_MCP
- Fallback triggered: **否**（首选成功）
- MCP mutation: `actor_move` axis=z delta=+5 → target `[4850, -3700, 655]`

### Readback (independent)
- After mutate: `[4850.0, -3700.0, 655.0]` — Executor verify `transform.location` passed，最大偏差 0.0000
- Evidence file: `logs/runs/phase3_demo_a_20260920-212159.json`

### Restore
- Absolute restore to `[4850, -3700, 650]` via UNREAL_MCP
- Independent readback: `[4850.0, -3700.0, 650.0]`
- Idempotent re-apply: still `[4850.0, -3700.0, 650.0]`（无重复偏移）

### Final
**RESULT = PASS**

---

## 3. Demo B 完整证据

| Actor | Semantic | Confidence | Recommended | Evidence |
|---|---|---|---|---|
| EXT_Plaza_Central | GROUND | 0.9 | ground_gap | keyword:GROUND |
| Hall_Floor | GROUND | 0.9 | ground_gap | keyword:GROUND |
| Platform_Skirt_Front | STRUCTURE | 0.8 | structure_trace | keyword:STRUCTURE |
| Roof_Main | STRUCTURE | 0.8 | structure_trace | **未被砸到地面** |
| Roof_EaveFront | STRUCTURE | 0.8 | structure_trace | 同上 |
| Porch_Beam | STRUCTURE | 0.8 | structure_trace | 梁柱走结构支撑 |
| Hall_Int_Beam_W | STRUCTURE | 0.8 | structure_trace | 同上 |
| EXT_MarkerPost_0_W | UNKNOWN | 0.2 | vision | 无名称信号 → needs_ai |

**不变量**：
- `roof_not_ground_forced = true`
- 纯 Decoration 不在本轮真实样本中出现；离线测试已覆盖「纯 Decoration → UNKNOWN / needs_ai」
- UNKNOWN → vision，不无条件放行

### line_trace（精确支撑面）
- Actor: Platform_Skirt_Front
- Start Z=700 → End Z=-4350
- Hit: `EXT_Terrace_Course1` location `[4850, -3700, 420.0]`, normal `+Z`
- distance=280

**RESULT = PASS**（分类+射线证据）；完整多对象 auto ground_place 批量修复本轮 **PARTIAL**（未批量改场景，避免污染正式关卡）。

Evidence: `logs/runs/phase3_demo_b_20260920-212443.json`

---

## 4. Demo C 完整证据

| 项 | 值 |
|---|---|
| Window | hwnd **1315442**（重启 UE 后；旧缓存 hwnd=660150 失效） |
| Calibration | layout_valid=false → **自动重标定** |
| ROI | world_outliner / details_panel / viewport / toolbar / content_browser 已更新 |
| focus_point | `[810, 437]` |
| Overlay | available=true（Python 3.12 tkinter 8.6） |
| DesktopLock | acquire=true |
| CU connect | available=true，tools=17 |
| GUI Action | click focus viewport `[810,437]`，duration≈1218ms，ok=true |
| Release | `release_control` ok；desktop lock released |
| Verification | CU 调用返回成功；锁释放后 lock holder 可空 |

**RESULT = PASS**

Evidence: `logs/runs/phase3_demo_c_20260920-212447.json`

---

## 5. Pause / Stop / EStop 真机测试

Evidence: `logs/runs/phase3_control_20260920-213622.json`（修复后复验）

| 控制 | 真机结果 |
|---|---|
| **Pause** | phase=PAUSED，`blocks_writes=true`；`actor_move` 失败：`控制状态阻止写/键鼠：PAUSED/PAUSE`；坐标保持 650 |
| **Resume** | phase 恢复；mutation 成功 → Z=652 |
| **Stop** | phase=STOPPING/command=STOP；mutation 失败：`任务已中止（STOP）` |
| **Emergency Stop（钩子路径）** | `on_emergency` 已注册并执行：`release_all_keys_and_buttons`（修饰键+字母+三键 up）、CU `release_control` **released=true, was=unreal-hybrid-agent**（peek 后 holder=null）、`coordinator.force_release_all()`；`desktop_released_by_hooks=true`（非 harness 代为释放） |
| **ESTOP 持久性** | `end_task(ok=False)` 后 phase 仍为 **ABORTED**，`should_abort=true`，后续 mutation 仍被拒 |
| **最终坐标** | 恢复至 650 |
| EStop 是否依赖 LLM | **否**（本地状态机 + 确定性钩子） |

**RESULT = PASS**（含钩子证据与 end_task 持久性）

---

## 6. Router Quality（真实样本）

`.state/router_quality.json` 摘要（`python uha.py quality`）：

| task_category | sample_size (tasks) | first_choice_success_rate | fallback_rate | avg_attempts | regret |
|---|---|---|---|---|---|
| actor_mutation | 11 | 1.00 | 0.00 | 1.0 | 0 |
| actor_read | 4 | 0.75 | 0.25 | 1.5 | 0 |
| level_save | 2 | **0.00** | **1.00** | 2.0 | **2** |
| visual_inspection | 1 | 1.00 | 0.00 | 1.0 | 0 |

**说明**：样本量很小，数字仅供初步观察，**不能**宣称 Router 已达高成功率。

真实 fallback 证据（最重要）：
- `level_save` 首选 **KEYBOARD/Ctrl+S**：接口报 success，但 **umap mtime 未变** → 验证失败
- Executor classify → fallback **UNREAL_MCP** → mtime `1789898960 → 1789910747` / 后续 `→ 1789910766`，dirty 1→0
- 这是真机闭环 fallback，不是 mock

健康度隔离：
- `actor_read::UNREAL_MCP` 失败（不存在的 Actor）**不会**封杀 `actor_mutation::UNREAL_MCP`（ok=11，failed=0）
- `UNEXPECTED`/`NOT_SUPPORTED` 不进健康度的设计保持；本轮 induced failure 记为真实 TOOL_CALL/VERIFICATION 失败

---

## 7. Performance（真机采样）

| 指标 | 观测 |
|---|---|
| Router decision time | 约 **592–2811 ms**（含 availability probe / MCP client 初始化） |
| MCP structured read (find/transform) | 约 **350–700 ms**/次 |
| MCP mutation + verify | 约 **0.9–2.3 s**/次 |
| level_save（含 fallback） | 约 **9.6–10.1 s**（KEYBOARD 失败路径较贵） |
| CU click | 约 **1.2 s** |
| GUI calibration cache | 窗口变化后 recalibrate；同布局可复用（offline 测 cache_hits 行为正常） |
| write lock wait | ≈0.0001s（本机负载低） |
| desktop lock wait | ≈0s |
| parallel read wall time | serial_wall≈1662ms vs parallel_wall≈1667ms → **speedup≈1.0** |

**并发结论**：锁层允许读并行、写串行；但 **UE MCP TCP/Editor 侧基本串行应答**，本机端到端读并行**没有墙钟收益**。瓶颈在 MCP/UE，不在 UHA 读锁。

---

## 8. Failed Tests / Remaining Problems

### Blocker
- 无（当前机器上 Structured UE 编辑闭环已打通）

### Major
1. **Router 对 `level_save` 的 KEYBOARD 首选仍不可靠**  
   - 现象：真机 Ctrl+S 路径常 success=true 但 mtime 不变  
   - 影响：每次 save 多耗 ~5s 且依赖 fallback  
   - 状态：fallback 能救回；**能力/健康度驱动的选路校准尚未做**（本轮按要求未改 Router 架构）
2. **全场景几何浮空检测误报仍高**  
   - `visual_inspect` 在 explicit ground=100 时标记 515/599（含 Roof 等合理高构件）  
   - SemanticResolver 在**逐对象分类**上正确，但巡检流水线若继续用统一地面几何，仍会把结构件当浮空  
   - 下一步：巡检默认叠加 semantic filter（GROUND 才做 ground_gap；STRUCTURE 走 structure_trace）
3. **batch_mutation 真机路径未完整验收**  
   - 质量账本尚无 `batch_mutation` 样本；限制 `max_actors_per_batch` 仅有策略

### Minor
1. `coordinator.stats().current_holders` 偶发滞后；修复后控制脚本收尾快照已可为空
2. 系统 Python 3.12 下 Overlay 线程测试偶发 `Tcl_AsyncDelete`（teardown 警告）
3. GUI ROI 为启发式比例，复杂 docking 布局下点击点可能偏移
4. `visual_inspect` 的 VLM 通道本机未配置（`vlmConfigured=false`）——按设计 skipped，不是 PASS
5. 日志曾误把 `.state`/`logs` 提交进 baseline commit；已在 phase3 分支 `git rm --cached`（文件仍在磁盘）
6. Git 已安装于 `C:\Program Files\Git\cmd\git.exe`，新 shell 可能未进 PATH
7. Demo B 真机样本覆盖 GROUND/STRUCTURE/UNKNOWN；ATTACHED/HANGING 主要靠离线测试（场景暂无吊灯命名对象）

### 评审后已修复（本轮 critical）
- `end_task` 清空 PAUSE/STOP/ESTOP → 已改为保留控制命令；`FAILED` 纳入 `blocks_writes`
- EStop 钩子未在真机证明 → 控制脚本按生产路径注册钩子并复验 **hooks_released_cu / desktop_released_by_hooks / estop_survives_end_task = true**
- Demo C 重标定后无条件 `valid=True` → 改为 fail-closed（无 ROI/fingerprint 则不通过）

### 测试运行注意
- `tests/test_phase2.py` 在 **system Python 3.12 + tkinter** 下可能因 Overlay 线程崩溃（exit 0x80000003）  
- 用项目 `.venv`（3.13，无 tk）跑 phase2 离线套件：**85/85 通过**  
- 真机 Demo 使用 system 3.12：Overlay 可用

---

## 9. 当前到底能不能日常使用

| 能力 | 判定 | 证据 |
|---|---|---|
| **Structured UE Editing** | **PASS** | Demo A：真机改 Actor + 独立回读 + 恢复 + 幂等重放；`level_save` 经 fallback 后 mtime/dirty 证据成立 |
| **Semantic Scene Inspection** | **PARTIAL** | Demo B 分类正确（Roof≠GROUND，UNKNOWN→vision，line_trace 可用）；但全场景几何浮空巡检误报仍高，尚不能无脑自动修复 |
| **Computer Use** | **PASS** | Demo C：标定→DesktopLock→click→release 全链成功；CU health ok |
| **Autonomous Hybrid Workflow** | **PARTIAL** | 结构化闭环+控制面可用；但 Router 在 GUI save 上首选偏差、批量与 VLM 路径未完成真机验收，混合长任务尚不宜无人值守 |

---

## 10. 下一步（只列真实剩余问题）

1. 用已积累的 quality/health 数据**校准 level_save 选路**（提高结构化 save 权重 / 对 KEYBOARD save 的冷却），仍保持单 Router。
2. 在 `visual_inspect` 默认路径接入 SemanticSupportResolver 过滤：GROUND→ground_gap，STRUCTURE/ATTACHED→trace，HANGING skip。
3. 真机跑 **batch_mutation** 小批量（≤5 Actor，可恢复）并写入 quality。
4. 为 GUI ROI 增加一次人工核对或自动 screenshot 对齐，减少启发式偏移。
5. 修复 lock stats holder 清理；Overlay 线程退出时序。
6. 可选：UE Remote Execution（UE_PYTHON）活体验证，作为 MCP 故障时的正式备份通道。

---

## 11. 测试计数

| 套件 | 通过 | 说明 |
|---|---|---|
| test_offline | **245/245** | `.venv` / 系统 Python 均可通过 |
| test_router | **24/24** | |
| test_phase2 | **85/85** | 使用 `.venv` Python 3.13（避免 tk 线程崩溃） |
| test_phase3 | **11/11** | pause/estop 持久性回归 + doctor 可导入 |
| **total** | **365/365** | |

真机 Demo / 控制证据（非 pytest）：
- doctor: gates.can_read_ue=true
- Demo A: PASS
- Demo B: PASS（分类+line_trace；批量 auto-place 仍 PARTIAL）
- Demo C: PASS
- Control: PASS（含钩子释放与 end_task 持久性）
- Quality ledger: 有真实样本（小样本）

---

## 12. 十问回答（验收）

1. **UHA 能不能真的读 UE？** **能。** doctor + Demo A before/mid/after 回读。  
2. **能不能真的改 UE？** **能。** Z 650→655→650 真机变更。  
3. **修改后能不能自己验证？** **能。** Executor verify + 独立 MCP 读。  
4. **错误操作能不能恢复？** **能。** 绝对坐标 restore + 幂等重放；EStop 后坐标保持/恢复。  
5. **Semantic Resolver 是否减少误判？** **分类层面是**（Roof 不被当 GROUND）；**全场景几何巡检仍会误报**。  
6. **MCP 失败后是否合理 fallback？** **是。** save KEYBOARD→MCP 成功；不存在 Actor 时多方法尝试且 health 分类隔离。  
7. **GUI 是否可接管并释放？** **是。** DesktopLock + CU + release_control。  
8. **Pause/Stop/EStop 是否真机有效？** **是（修复并复验后）。** Pause 拒写；Stop/EStop 拒写；EStop=ABORTED 且钩子真机释放 CU/键鼠/锁；`end_task` 不再冲掉控制状态。  
9. **Router 第一次选择有多靠谱？** mutation 11/11 首选成功；save 0/2 首选成功（靠 fallback）；**样本小，仅初步观察**。  
10. **连续运行是否稳定？** 短序列稳定；**长时无人值守尚 PARTIAL**（GUI 首选偏差 + 批量未验收）。

---

## 13. 日志与证据路径

- Layered doctor: `logs/runs/phase3_doctor_*.json`
- Demo A: `logs/runs/phase3_demo_a_*.json` + `logs/runs/phase3_demo_a-*.jsonl`
- Demo B: `logs/runs/phase3_demo_b_*.json`
- Demo C: `logs/runs/phase3_demo_c_*.json`
- Suite: `logs/runs/phase3_suite_*.json`
- Control: `logs/runs/phase3_control_*.json`
- Quality: `.state/router_quality.json`
- Health: `.state/routing_stats.json`
- Spec: `docs/compose/spec/phase3-real-acceptance.md`

日志设计避免写入 API Key/Token；本地路径仅出现在本机验收文档中。

---

**结论（一句）**：Phase 3 在本机把 UHA 从「代码存在」推进到「结构化 UE 读写 + 回读验证 + 真实 fallback + GUI 接管/释放 + 控制面可用」；Router 质量账本开始有真实数据。尚未达到「混合工作流可无人值守日常使用」，主要缺口是 save 首选策略、语义过滤接入巡检、以及批量/GUI 精度。
