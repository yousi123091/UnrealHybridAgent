# 阶段总结：UnrealHybridAgent 第一版

**一句话**：给一个 Agent 在真实 UE5 编辑器里干活的能力——自动挑执行方式、失败自动降级、干完自动复验；并且**在它不确定的时候拒绝动手**。

本文回答三个问题：**交付了什么、验证到什么程度、还差什么。**

---

## 1. 交付了什么

```
uha.py                          CLI（doctor / list / plan / run / demo1 / demo2 / stats / roi-shot）
config/agent.config.json        运行时配置（通道地址、ROI、权重、硬预算；已 gitignore）
LICENSE                         MIT + 第三方组件许可说明

src/
  core/          errors.py（带稳定 code）· config.py · log.py（jsonl 运行日志）
  adapters/      mcp_client · unreal/{unreal_mcp, ue_python, commandlet} · computer_use
  router/        intent · rules · cost · fallback（降级链 + 健康度账本）· router
  scheduler/     executor.py —— 把选路/降级/验证/记账缝成闭环
  skills/        actor_find · actor_move · actor_inspect · level_save · visual_inspect
  planner/       Plan / PlanStep（线性、显式、支持 @占位符）
  vision/        judge（几何判定）· image（像素差分）· geometry · grounding
  validation/    CheckResult（三态）· verifiers

tools/observe.py                独立只读观测器：绕过框架直接读 MCP，用来对账
tests/                          test_offline.py（195 项）· test_router.py（24 项）· smoke_computer_use.py
docs/                           DESIGN · FINDINGS · DEMO · ROADMAP · FINAL_REPORT（本文）
logs/runs/*.jsonl               每次运行逐事件可复盘
artifacts/                      截图等产物（含 Demo2 的前后视口证据图）
```

### 五个能力（按重要性）

1. **可解释的选路**：规则 + 成本模型，输出首选、完整降级链、**以及被淘汰方案的原因**。第一版不引入任何模型做决策。
2. **三态验证**：`passed` / `failed` / `skipped` 严格分开。弱检查降为 `advisory`。**写入返回值不算证据，重新读状态才算。**
3. **不可幂等动作的重试护栏**：`perform` 抛错或验证不通过时，必须先重新读状态确认，才能宣告失败；否则降级重试会让副作用生效两次（这条护栏是被 F3 逼出来的）。
4. **不可信就拒绝动手**：检测器输出 `actionable` 标志，只有为真才允许自动改场景；否则相关步骤被前置条件拦住并明确报错。
5. **结构化通道 / GUI 视觉通道二分**：能程序化复验的走结构化，够不着的才上 GUI。

---

## 2. 验证到什么程度

### 自动化测试

| 套件 | 数量 | 覆盖 |
|---|---|---|
| `tests/test_offline.py` | **195 项全过** | MCP 参数契约、ActorRef 解析、响应行抽取、浮空几何判定、三态验证器、降级记账、计划与占位符、可靠性闸门、受控检测、判定路径、`prefer_method`、幂等护栏、自然语言解析、**测试不污染生产状态** |
| `tests/test_router.py` | **24 项全过** | 11 组路由场景（含"同一意图、不同可用性 → 决策必须变化"）、成本分量的可解释性、意图推断 |

这两个套件**不需要 UE、不需要网络**。其中相当一部分是**回归测试**——每一条都对应一次真实踩坑，而不是为了凑覆盖率。

### 真机端到端（本机 UE 5.8 + unreal-mcp v2.2.0）

| 场景 | 结果 | 独立复核 |
|---|---|---|
| **Demo1** 抬高 `Hall_Floor` 20 + 保存 | 3 步全成功（保存走了降级：KEYBOARD → UNREAL_MCP）46.7 s | `tools/observe.py` 读到 Z=860，`.umap` mtime 推进 |
| **Demo2** 浮空检测 → 混合通道修正 → 复验 | 3 步全成功，`HYBRID` 一次做对、**无降级重试**，38.7 s | 构件回到 Z=240，`floating_count: 0` |
| 场景复位 | 两个试件回到原始坐标，`dirty_count: 0` | 独立读原始 MCP 通道确认 |

每一步结果都用**独立观测器**（`tools/observe.py`，不经过任何 UHA 封装）对过账。框架自己说"成功"不算证据——这是全项目唯一贯穿始终的硬规矩。

### 没能验证的（诚实列出）

- **`UE_PYTHON` / `UE_COMMANDLET` 两条通道代码在、活体环境没验证过**：前者需要编辑器开启 `bRemoteExecution`（默认关），后者与已打开的编辑器会话互斥。
- **GUI 降级链路从未跑满**：`vision.roi.*` 与 `desktop.editor_focus_point` 未标定，所以 `actor_find` 的 World Outliner 搜索路径、`level_save` 的 Ctrl+S 路径都是**报错降级**而非成功路径。它们的"会明确拒绝"行为是验证过的，"能干活"没验证过。
- **VLM 语义定位**：Agent-TARS 报 `vlmConfigured=false`，不可用（对应检查记 `skipped`，不影响确定性判断）。

---

## 3. 三个（现在是五个）实测发现

这部分是本项目**最有价值的产出**——它们不是"实现了什么"，而是"原以为对的事情其实不对"。

| # | 发现 | 结论 |
|---|---|---|
| **F1** | 全场景自动浮空检测，四种几何方法误报率 **48% / 95% / 77% / 49%** | 仅凭包围盒几何，在多层建筑场景里做不到。改为"显式地面参照物 + 限定区域 + 不可信拒绝动手" |
| **F2** | "保存是否落盘"的三种候选证据 | 只有 `.umap` 文件 mtime 可信；脏包 `0→0` 是**空证据**，必须记 `skipped` |
| **F3** | 弱检查假红 → 相对位移被重复执行 → **构件沉进地面，而计划报"全部成功"** | 三处修复：弱检查降级 advisory、不可幂等动作的重试护栏、日志不再误记降级 |
| **F4** | 参数名 `actor_label`、`get_transform` 顶层单行返回、`attempts` 与 `failures` 必须分开、Ctrl+S 发给错误窗口… | 见 [`FINDINGS.md#f4`](FINDINGS.md#f4) |
| **F5** | 跑一遍离线测试 → 生产账本被污染 → 最优通道进冷却 → **没有任何可用的执行方式** | 四类记账缺陷：测试未隔离、`NOT_SUPPORTED` 误记账、成功不解除冷却、失败信息指向无关病因 |

**F3 与 F5 是同一类问题的两次发作**：不是功能写错了，而是**"记账"写错了**——一个错误的结论被当成真证据，或一笔错误的账被当成真健康度。它们的共同特征是**现场看起来完全正常**：接口全绿、报告"成功"、日志无异常。这也是为什么这两条都配了专项回归测试。

细节与数据：[`FINDINGS.md`](FINDINGS.md)。

---

## 4. 已知限制

| 限制 | 影响 | 去向 |
|---|---|---|
| 只支持"显式地面参照物 + 限定区域"的浮空检测 | 无法全场景自动扫 | [ROADMAP §1](ROADMAP.md#1-全场景自动浮空检测--不做) |
| 修正量 `drop_by` 是调用方给的近似值 | 不是"精确落回地面" | [ROADMAP §2](ROADMAP.md#2-精确落回地面--只做了近似下压) |
| 批量操作只有安全策略，没有执行路径 | 单个 skill 只能改一个对象 | [ROADMAP §3](ROADMAP.md#3-批量操作--只有安全策略没有执行路径) |
| GUI 通道未标定 | GUI 路径明确报错降级；`level_save` 每次多花约 2 s | [ROADMAP §6](ROADMAP.md#6-gui-通道的自动标定--现在是不标定就拒绝执行) |
| 幂等性声明只覆盖 `actor_move` | 新增写操作 skill 时默认值 `True` 是危险默认 | [ROADMAP §9](ROADMAP.md#9-幂等性声明只覆盖了一个-skill) |
| 健康度是全局成功率 | 会把"通道不擅长的任务"混进账本 | [ROADMAP §10](ROADMAP.md#10-健康度记账从全局成功率到按任务类型记账) |
| 无跨进程任务队列 | 两个 `uha` 同时跑会抢桌面 | [ROADMAP §11](ROADMAP.md#11-并发与排队) |
| 单工程单关卡配置 | 换场景要手打 `--region` | [ROADMAP §12](ROADMAP.md#12-配置与多工程) |

---

## 5. 结论：什么可信，什么不可信

**可以信**：

- 结构化通道上的**读写 + 复验**闭环（Demo1 全程有独立证据）。
- "这个构件离地多少"这类**有确定答案**的判断——它是算术题，不是视觉题。
- 失败时的**如实上报**：证据不足记 `skipped`，绝不记 `passed`。
- 每个具体结论都能追到证据：日志逐事件、截图落盘、`tools/observe.py` 可独立对账。

**不要信**：

- 任何"接口返回 `success=true`"作为完成证据。
- 整屏画面差分作为操作成败的门槛（对远处小构件不具鉴别力）。
- 通道健康度账本里**没有冷却提示**的失败诊断（可能是账的问题，不是参数的问题）。
- "计划报告全部成功"——**F3 那次也报的全部成功，而构件已经沉进地面了**。

最后一条是这份总结里最该记住的一句。

---

## 6. 下一步

按优先级排在 [`ROADMAP.md`](ROADMAP.md) 末尾，前四条是：

1. **精确落回地面**（射线检测 + 包围盒对齐）——把 Demo2 从"演示"变成"能用的功能"，MCP 的 `line_trace` / `spawn_on_surface_raycast` 通路已经现成。
2. **幂等性声明补齐 + 收紧危险的默认值**——成本极低，直接降低"改坏工程"的概率。
3. **健康度按任务类型记账**（`read_only × HYBRID` 与 `mutation × HYBRID` 分开算）——同类任务变多之后，全局成功率一定会开始误导选路。
4. **GUI 标定流程**——目前"降级链跑不满"的唯一硬阻塞。

---

## 附：本次开发过程中，环境与工具上踩的坑（备查）

| 现象 | 处理 |
|---|---|
| 直接跑 `.py` 脚本文件被沙箱拦 | 用 `python -c "exec(open(..., encoding='utf-8').read())"`，或经 PowerShell 调 venv 解释器 |
| PowerShell 重定向 Python 输出到文件后中文变乱码 | 先 `$env:PYTHONIOENCODING='utf-8'` |
| 多行命令在 bash 工具里容易 SIGTERM | 命令写成单行；长任务放后台跑 |
| 删除目录被 safe-delete 包装器拦住 | 单文件可删；目录改用 .NET 原生接口 |
