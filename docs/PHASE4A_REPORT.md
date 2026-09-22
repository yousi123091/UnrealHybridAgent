> Public edition: project names are anonymized; historical measurements are retained.

# Phase 4A Acceptance Report — Daily Usable Runtime

**Machine**: Windows 11 · UE 5.8.2 · Project `ExamplePalace`  
**Branch**: `phase4a` · Baseline: `fb34270` (phase3)  
**Date**: 2026-09-20  
**Default execution mode**: `CONFIRM`（安全默认；CLI/config 可切 AUTO）

---

## 1. Acceptance Matrix

| Capability | Code | Offline Test | Real UE Test | Result |
|---|---|---|---|---|
| ExecutionMode AUTO/CONFIRM | ✅ | ✅ test_phase4 | Demo A：CONFIRM 拒写 / DENY / ALLOW / AUTO 安全写 | **PASS** |
| ApprovalGate (ALLOW/REQUIRE/DENY) | ✅ | ✅ | Demo A 真机；删除类 DENY 策略 | **PASS** |
| Semantic-first inspection | ✅ | ✅ | Demo B：Roof/Platform=STRUCTURE+trace；GROUND gap；UNKNOWN 禁改 | **PASS** |
| Geometry-only FP reduction | ✅ | ✅ | 真机 515/599（Phase3）→ **13/296**（语义过滤后） | **PASS** |
| Batch mutation pipeline | ✅ | ✅ | Demo C：3 Actor +2cm，readback 3/3，mcp_calls=10，wall≈2.0s | **PASS** |
| Batch absolute restore | ✅ | ✅ | Demo C restore 被 n=4 Approval 拦下；随后工具直接绝对恢复成功 | **PARTIAL** |
| level_save structured first | ✅ | ✅ | Demo D：**UNREAL_MCP first choice**，mtime/dirty 证据，无 fallback | **PASS** |
| Capability cache | ✅ | ✅ | Demo E：decide cold≈340ms → warm≈0.11ms | **PASS** |
| Task checkpoint / rollback | ✅ | ✅ | Demo F：absolute restore 650←659，独立回读 | **PASS** |
| Provider config section | ✅ | ✅ | `providers` 已写入 config；doctor/capabilities 可读 | **PARTIAL**（未做 UI） |
| GUI confidence gate | ⚠️ | partial | 配置键已加；完整 screenshot confidence 未在本轮深测 | **PARTIAL** |
| Pause control regression | ✅ | ✅ | Demo 安全段：PAUSE 拒写 | **PASS** |
| Phase3 control durability | ✅ | ✅ test_phase3 | end_task 不清 PAUSE/STOP/ESTOP | **PASS** |

---

## 2. Architecture Changes

### ExecutionMode + ApprovalGate
- `src/core/execution_mode.py`
- Modes: `AUTO` | `CONFIRM`（默认 CONFIRM）
- Decisions: `ALLOW` / `REQUIRE_CONFIRMATION` / `DENY`
- Risk-based：读操作放行；`level_save` 低风险放行；UNKNOWN/不可逆/大批量/高风险需确认
- AUTO 含义 = **自动做安全决策**，不是跳过安全
- CLI: `python uha.py run-text "..." --mode auto|confirm [--approve]`

### Semantic Inspection
- `src/vision/semantic_inspect.py` + `visual_inspect` 主流程
- 禁止默认「Z 高 → 浮空 → 砸地」
- GROUND → ground_gap；STRUCTURE/ATTACHED → line_trace 支撑；HANGING skip；UNKNOWN → REQUIRE_CONFIRMATION
- ground-gap 浮空候选 **只保留高置信 GROUND**

### Batch Pipeline
- `src/skills/batch_mutation.py`
- 一次 capability probe → checkpoint → approval → batch write → batch readback
- 默认上限 `safety.batch_default_limit=20`；AUTO 自动批 ≤3
- 失败不自动相对重试（防 double movement）；绝对恢复走 checkpoint/locations

### Capability Cache
- `src/router/capability_cache.py`
- Session TTL（默认 20s）；失败 invalidate；不掩盖故障

### Save Router
- `rule_structured_save` bias 0.48；`save_shortcut` bias 降至 0.12
- Health/quality 驱动降权（连续失败/低成功率）
- 验证仍以 **mtime + dirty** 为准

### Checkpoint / Rollback
- `src/core/checkpoint.py`
- `.state/checkpoints/<task_id>.json`
- Rollback 使用 **absolute location**，禁止 reverse-delta
- CLI: `python uha.py rollback <task-id>` / `checkpoints`

### Provider Config
```json
"providers": {
  "unreal": {"type": "genorca_mcp", "ref": "mcp_servers.unreal_mcp", "ue_tcp_port": 12029},
  "desktop": {"type": "agent_tars", "base_url": "http://127.0.0.1:8788"},
  "vision": {"type": "heuristic"}
}
```

---

## 3. Performance（真机原始数据）

| 指标 | 数值 |
|---|---|
| Router decide **cold**（无 capability cache） | **340–393 ms**（均值 ≈355 ms） |
| Router decide **warm**（cache hit） | **0.10–0.11 ms** |
| Capability probe cold | **328–374 ms** |
| Capability probe warm | **0.014–0.016 ms** |
| Single actor_move（结构化+verify） | **≈1.30–1.34 s** |
| Batch 3 actors（写+回读 verify） | **wall_ms=1998**；executor 端到端 ≈4.0 s |
| Batch MCP round trips（3 actors） | **10**（含 before capture / write / readback） |
| level_save Phase4A | **≈3.76 s**，first=UNREAL_MCP，fallback=否 |
| level_save Phase3 基线 | **9.6–10.1 s**（KEYBOARD 首选失败再 fallback） |
| Save 提升 | **约 2.5×**（绝对：约 −5.8 s/次） |
| Batch 4 actors restore 被 Gate 拦 | Approval REQUIRE（n>auto_allow_max_batch=3） |

> 注意：Router 优化数字是 **decide/probe 层**，不含 MCP/UE 业务耗时。

---

## 4. Safety（真机）

| 项 | 证据 |
|---|---|
| Pause | CONFIRM/AUTO 写任务在 PAUSED 被拒；坐标不变 |
| Stop / EStop | Phase3 回归 test_phase3 11/11；end_task 不冲掉控制态 |
| Approval reject | Demo A：`REQUIRE_CONFIRMATION` / `DENY 用户拒绝` → **无 mutation** |
| UNKNOWN fail-safe | Demo B probes：UNKNOWN → REQUIRE_CONFIRMATION，不自动改 |
| Rollback | Demo F absolute restore + 回读一致 |
| 不污染正式场景 | 测试 Actor 已恢复：Skirt/Marker 基线坐标 |

---

## 5. Demo Evidence Summary

### Demo A — Execution Mode
- CONFIRM 无批准：失败，`ApprovalGate REQUIRE_CONFIRMATION`
- 用户拒绝：失败，`DENY: 用户拒绝`
- 用户允许：成功，MCP verify 通过
- AUTO 安绝对恢复：成功
- **Result: PASS**

### Demo B — Semantic Floating
真机抽样：
| Actor | Semantic | decision | support |
|---|---|---|---|
| Hall_Floor | GROUND 0.9 | OK_ON_GROUND | hit Hall_AxisStrip |
| Platform_Main | STRUCTURE 0.8 | STRUCTURE_SUPPORTED | hit EA_LandFoundation |
| Platform_Skirt_Front | STRUCTURE 0.8 | STRUCTURE_SUPPORTED | hit EXT_Terrace_Course1 |
| Hall_AxisStrip / DoorJamb_* | UNKNOWN 0.2 | REQUIRE_CONFIRMATION | — |

全场景（ground_ref=EXT_Plaza_Central，sample 296）：
- 旧几何口径（Phase3）：**515/599** 疑似浮空
- 语义过滤后：**13/296**（4.4%），且主要是抬高的 Corridor Floor（GROUND 名）
- 语义不会把 Roof 当 ground 浮空
- **Result: PASS**（误报显著下降；非零误报如实保留）

### Demo C — Batch
- targets: Platform_Skirt_Front/West/East
- before → +2cm → mid 回读一致
- **batch_ok=true**，success_count=3/3，retry=0，fallback=0
- mcp_calls=10，perform wall_ms≈1998
- restore：4 对象批量被 Approval 拦（安全策略）；工具层绝对恢复成功
- **Result: PASS（批量执行） / PARTIAL（CLI 恢复路径需 --mode auto+approve 或 n≤3）**

### Demo D — Save
- Router first choice: **UNREAL_MCP**
- latency ≈ **3764 ms**（vs Phase3 9600–10100 ms）
- verify: mtime 推进 + dirty 1→0
- fallback: **否**
- **Result: PASS**

### Demo E — Capability Cache
- decide cold ≈ **340 ms** → warm ≈ **0.11 ms**
- cache hits>0，failures 可 invalidate
- **Result: PASS**

### Demo F — Checkpoint/Rollback
- origin Z=652 → mutate → dirty 659 → rollback absolute → **652**
- independent readback verified
- **Result: PASS**

---

## 6. Failed Tests / Remaining Problems

### Major
1. **Batch restore via skill in CONFIRM/AUTO with n>3** 会被 Approval 拦住（设计如此），演示脚本需分批或 `--approve`。
2. **GUI ROI confidence** 仅配置骨架，未做 screenshot-based sanity 真机闭环。
3. **Provider 层**仍是 config 映射，不是完整 Gateway/UI；backend replacement 未做多 provider 热切换实测。
4. **长时无人值守**仍不推荐：UNKNOWN 多、GUI ROI 启发式、batch 需策略边界。
5. **Corridor Floor 等 GROUND 抬高件**仍会进入 ground-gap 候选（语义正确但业务上可能合法多层地坪）。

### Minor
1. Router quality 中 batch 历史失败样本会短期降权（健康度工作正常，但需成功样本稀释）。
2. `uha.py batch --absolute-z` CLI 仍提示用 tools 脚本（未完全实现绝对批量 CLI）。
3. Overlay/tk 线程告警仍在系统 Python 测试路径偶发。
4. Git 安装后新 shell 可能需全路径：`<GIT_EXE>`

---

## 7. Test Counts

| Suite | Result |
|---|---|
| test_offline | **248/248** |
| test_router | **25/25** |
| test_phase2 | **85/85** |
| test_phase3 | **11/11** |
| test_phase4 | **13/13** |
| **total** | **382/382** |

真机证据：`logs/runs/phase4a_evidence_20260920-222429.json`（及同日前序）

---

## 8. Current Daily Usability

1. **是否适合日常用于当前 UE 项目？**  
   **有条件适合（PARTIAL → 可用）**：结构化读写、保存、小批量修改、语义巡检、检查点回滚、审批门已在真机跑通。适合**有边界、可回滚**的日常场景开发辅助。

2. **哪些任务可以全托管（AUTO）？**  
   - 已知 Actor 的小位移/绝对坐标修改（≤3，有 checkpoint）  
   - level_save（结构化 API）  
   - 只读 inspect / semantic inspection  
   - 批量 ≤ AUTO 上限且可绝对恢复  

3. **哪些任务仍必须人工确认？**  
   - 批量 >3（CONFIRM）或 >5（阈值）  
   - UNKNOWN/低置信语义对象  
   - 删除/覆盖资源（当前直接 DENY）  
   - GUI 精确点击 / 复杂 docking  
   - 无法保证 rollback 的操作  

4. **最大剩余风险**  
   - 语义 UNKNOWN 覆盖仍不少（door/throne/marker 等）  
   - GUI ROI 启发式  
   - 多层建筑 GROUND 语义与业务“合法抬高”的边界需人工策略  
   - 无人值守长任务仍缺持续审批/异常看板  

5. **当前单任务推荐最大 Actor 数量**  
   - **AUTO：≤3**  
   - **CONFIRM：≤5～20（默认 batch_default_limit=20，超过需确认）**  

6. **长时间无人值守是否已经适合？**  
   **尚未完全适合**。短时、可回滚任务可以；过夜级无人值守仍不建议。

7. **下一阶段最值得做什么**  
   - 语义规则按工程 profile 固化（Door/Throne/Marker/CorrFloor）  
   - GUI screenshot confidence 真机校准  
   - Approval 的轻量 UI（复用 Overlay）  
   - 用 quality ledger 自动冷却 KEYBOARD save / 失败 batch  
   - Provider 热切换与 per-backend doctor  

---

## 9. Git

- Branch: `phase4a`
- Baseline: `fb34270`
- 建议提交信息：
  - `feat: add execution modes and approval gate`
  - `fix: integrate semantic support into scene inspection`
  - `feat: implement verified batch mutation`
  - `perf: cache backend capabilities`
  - `fix: route level save to structured backend`
  - `feat: add task checkpoint and rollback`

（实际本轮实现可能合并为一个或多个 commit；不提交 `.state/`、logs、密钥。）

---

**结论**：Phase 4A 已在当前真实 Unreal 工程上证明：**审批门、语义巡检、结构化保存、能力缓存、检查点回滚、小批量 mutation** 可工作。UHA 具备“日常可控使用”的核心条件，但**不是**无监督全自动生产机器人。
