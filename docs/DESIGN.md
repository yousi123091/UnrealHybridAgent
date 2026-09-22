# 设计说明

本文说明 UHA 的分层、数据流，以及**为什么这么分层**。重点在取舍理由，代码细节看源码注释。

---

## 1. 分层

```
core/        错误（带稳定 code）· 配置 · 运行日志
  ▲
adapters/    协议适配：把"某种通道"翻译成统一的 op 词汇表
  ▲
router/      intent → rules + cost → Decision（首选 + 降级链）
  ▲
scheduler/   Executor：一次任务的生命周期闭环
  ▲
skills/      语义动作：actor_find / actor_move / level_save / visual_inspect ...
planner/     把目标编成 Plan（线性、显式）
vision/      几何判定 + 像素差分
validation/  CheckResult 三态 + 各种 verifier
```

依赖方向**单向向下**：`skills` 不 import `router`，`adapters` 不 import `scheduler`。这样 router 的规则可以单独测（`tests/test_router.py`），adapters 可以单独探针验证（`tools/observe.py`）。

---

## 2. 统一 op 词汇表（adapters 层）

任何 UE 侧后端都要实现同一组 op，名字和返回结构一致：

| 统一 op | GenOrca MCP 的真实工具 |
|---|---|
| `get_actors` | `actor {action: get_all_details}` |
| `get_actor_transform` | `actor {action: get_transform}` |
| `set_actor_location` | `actor {action: set_location}` |
| `save_level` | `level {action: save_current_level}` |
| `level_dirty_state` | `util {action: execute_python}` |
| `level_file_stat` | `util {action: execute_python}` |
| `viewport_screenshot` | `vision {action: capture_viewport}`（返回 MCP **Image** 块，不是 text） |

这样写是为了**换后端时上层零改动**。Router 换成 UE_PYTHON 时，`actor_move` 一行都不用动。

> 实测要点（v2.2.0 / UE 5.8）：actor 侧参数名是 `actor_label`（不是 `actor_name`）；
> actor 域**没有** find 动作，查找靠 `get_transform` 命中或全表过滤；
> `get_transform` 的返回是**顶层单行**（不是嵌在某个 list 里）——这一点曾导致
> "未返回 transform" 的诡异报错，根因就是行抽取只找了嵌套结构。

---

## 3. Router：规则 + 成本，且必须可解释

第一版**不引入任何模型**。每条规则是 if-then，输出人类可读的 `reason` 直接进日志：

```
rule_hybrid_for_vision   既要看又要精确改   → HYBRID      bias 0.55
rule_vision_required     含主观判断         → VISION      bias 0.50
rule_batch               >=5 个对象         → UNREAL_MCP  bias 0.50
rule_exact_values        需要精确数值       → UNREAL_MCP  bias 0.35
rule_read_only           只读查询           → UNREAL_MCP  bias 0.30
...
```

决策是 `score - bias` 取最小，其中 `score` 来自成本模型（cost / latency / precision / risk / success 五个加权分量）。日志里会同时写下**被淘汰方案的原因**，出问题能一眼看出是哪条规则或哪个成本分量选错了。

### 步骤级建议（`prefer_method`）

计划里的某一步可以**建议**用某条通道。这是一个容易和"不要在两层同时做决策"打架的东西，所以边界写死：

- 它**只调整尝试顺序**，不删减降级链——建议的方式失败后照样按 Router 的顺序继续降级；
- 建议的方式不在候选里（后端不可用 / 冷却中）→ **忽略并记日志**，不硬闯。

为什么仍然需要它：当某一步的**目的就是演练某条通道**时（演示、通道契约测试），"按成本挑最快"是错的目标函数。实测 Demo2 的搬运步骤如果不加这个建议，Router 会选成本更低的 `UNREAL_MCP`，那样"混合通道修正"就没被真正跑到。

---

## 4. Executor：一次任务的生命周期

```
1. skill.intent()      参数 → 结构化意图
2. router.decide()     首选 + 理由 + 完整降级链
3. skill.preflight()   采基线（用来做前后对比）
4. skill.perform()     执行
5. skill.verify()      **重新读状态**复验，不信任写入返回值
6. 不通过 / 抛错 → 换下一种方式，回到 4
7. 全失败 → 如实返回失败，每轮原因都留在 attempts 里
```

三个刻意选择：

- **验证不通过 = 失败**，和"抛异常"同级。自动化里最危险的状态是"接口说成功了、实际没生效"。
- **降级有硬预算**（每方法 2 次失败、总 5 次），避免在注定不通的路上耗到超时。
- **失败原因结构化**（用 `error.code` 而不是错误字符串），跨版本跨语言都稳定。

### 记账的一个坑

`FallbackState` 里 `attempts`（总尝试）和 `failures`（失败）**必须分开**。只算总尝试会让一个连续成功的通道在跑满两次后"被耗尽"，后续步骤直接跳过它——Demo1 里真的出现过"UNREAL_MCP 成功两步后，第三步保存被跳过"。

### 不可幂等动作的护栏

见 [`FINDINGS.md#f3`](FINDINGS.md#f3)。要点：`perform` 抛错**不等于没生效**，所以任何动作在宣告失败前都要问一句"目标状态现在满足了没有"；对**不可幂等**的动作（相对位移）这一步是安全要求，因为一旦误判成失败，降级重试就会让副作用生效两次。

---

## 5. 验证：三态，以及"什么才算证据"

`CheckResult` 有 `passed` / `skipped` 两个独立的位，组合出三种结论：

| | `passed=True` | `passed=False` |
|---|---|---|
| `skipped=False` | **通过** | **不通过** → 触发降级 |
| `skipped=True` | — | **没验成**（证据不足）→ 如实上报 |

`VerifyReport.passed` 只看**已执行**的检查。所以"全部跳过"会被判为 `inconclusive`，而不是通过。

### 证据的强弱

以"保存 Level 到底有没有落盘"为例，实测过三种证据：

| 证据 | 可信吗 | 原因 |
|---|---|---|
| 保存 API 返回 `success=true` | ✗ | 只是"调用没报错" |
| 编辑器内部脏包计数 | ✗ 单用时 | UE 5.8 上 `get_dirty_map_packages()` **不反映 Python 侧修改**，写入后仍是 0 → 判"通过"是假绿 |
| **`.umap` 文件 mtime 推进** | ✓ | 进程外证据：与编辑器内部状态无关，写入不动它、保存才动 |

所以 `level_save` 现在把 mtime 当**主证据**，脏包检查降为辅助（且带"空证据闸门"：`before==0 && after==0` 判 `skipped` 而不是 `passed`）。

---

## 6. Planner：线性、显式

计划是**线性且显式**的，不做动态重规划：出问题时一眼能看出是哪一步错了；每一步写清 skill、参数、能不能跳过、依赖谁。

真正需要动态决策的地方只有"这一步用哪种方式执行"——那件事已经由 Router 负责了。**不要在两层同时做决策，会互相打架。**

步骤之间共享 `shared` 上下文，支持 `@名字` 占位符，让"先看再改"的链路不必人肉抄结论：

```python
visual_inspect 产出 floating_top  →  下一步的 actor 写 "@floating_top"
```

占位符解析不出来就**报错**，不做"默默用 0 代替"——那会变成一次静默的错误操作。

---

## 7. 日志与复盘

每次运行一份 `logs/runs/<run-id>.jsonl`，逐事件可复盘：

- `routing` —— 完整决策过程（含被淘汰方案与原因）
- `method_preference` —— 步骤级建议是否生效（`reordered` / `already_selected` / `ignored_not_available`）
- `step` —— 每一步的尝试：参数、结果、验证报告、耗时、错误
- `fallback` / `fallback_skip` / `fallback_budget_exhausted` / `method_exhausted`
- `plan_start` —— 整个计划的步骤清单

`tools/observe.py` 是**独立的只读观测器**，直接问 MCP 原始通道，用于跟框架的报告对账——框架自己说"成功"不算证据。
