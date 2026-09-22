> Public edition: project names are anonymized; historical measurements are retained.

# 实测发现

这份文档记录的是**真机跑出来的、和最初设想不一样的东西**。每一条都带数据。

环境：UE 5.8 编辑器 + GenOrca/unreal-mcp v2.2.0（127.0.0.1:12029），
测试关卡 `ExamplePalace`（示例宫殿风格场景，1184 个 Actor）。

| # | 一句话 | 严重度 |
|---|---|---|
| [F1](#f1) | 全场景自动浮空检测：四种几何方法全部失败（误报 48%~95%） | 方法结论 |
| [F2](#f2) | "保存有没有落盘"只有 `.umap` 的 mtime 可信 | 证据强度 |
| [F3](#f3) | 弱检查假红 → 相对位移被重复执行 → **真的改坏了场景** | **最严重** |
| [F4](#f4) | 其它踩过的坑（参数名、行抽取、记账、热键…） | 简记 |
| [F5](#f5) | 记账缺陷的连锁反应：跑一遍测试 → 没有任何可用的执行方式 | **次严重** |

> F3 与 F5 是同一类问题的两次发作：**不是功能写错了，而是"记账"写错了**——
> 一个错误的结论被当成真证据（F3：弱检查判失败），或一笔错误的账被当成真健康度（F5）。
> 这类缺陷的共同特征是**现场看起来完全正常**：接口全绿、报告"成功"、日志没有异常。

---

<a id="f1"></a>
## F1. 全场景自动浮空检测：四种方法全失败

**最初的设想**：扫一遍场景，用几何数据找出"悬空"的构件。
**实测结论**：仅凭包围盒几何，在多层建筑场景里**做不到**。

### 场景的真实结构

先把"底边 Z"的分布拉出来看（1179 个有包围盒的 Actor）：

```
min = -250.0   max = 3800.0   跨度 4050cm

 p 1 =    -95.0      p25 =    200.0      p75 =    935.0
 p 5 =    -40.0      p50 =    856.0      p95 =   2160.0
p10 =      0.0       p90 =   1980.3      p99 =   2295.0
```

底边直方图（每 200cm 一档，只列主要档）：

```
[  -200,     0) :    69
[     0,   200) :   218   ← 地坪层
[   400,   600) :   151
[   800,  1000) :   392   ← 主殿台面（最大峰）
[  1200,  1400) :    58
[  2000,  2200) :    81   ← 屋顶层
```

**分布是多峰的**。这不是一个"地面 + 一堆东西"的场景，而是**多个真实楼层**：
地坪 ≈ 0、主殿台面 ≈ 856、屋顶 ≈ 2000+。**不存在统一的"地面"。**

### 四种做法，全部失败

真值：**没有任何东西浮空**（那栋楼本来就长这样）。所以"判出多少个"就是纯粹的误报数。

| # | 方法 | 判为浮空 | 误报率 | 为什么失败 |
|---|---|---|---|---|
| ① | 全局中位数当地面 | 569 / 1179 | **48.3%** | 中位数落在主殿台面（856），于是台面以下/以上的都被判歪 |
| ② | 全局第 5 分位当地面 | 1117 / 1179 | **94.7%** | 低分位贴到了地形层（-40），于是几乎整栋建筑"都高于地面" |
| ③ | 局部邻域低分位 | 909 / 1179 | **77.1%** | 建筑里**同一个水平位置**就叠着柱子(840)、墙、屋顶(2000) |
| ④ | 物理定义：正下方支撑面 | 582 / 1179 | **49.4%** | 最接近真相，但 gap 是**连续谱**，没有可切的断点 |

方法 ④ 的定义是最严格的：对每个 Actor，取 XY 有重叠、顶面在其底边之下、顶面最高的那个作为支撑面。它的结果最有信息量：

```
support(A) = max{ B.top : B≠A, XY 与 A 相交, B.top <= A.bottom + ε }
gap(A)     = A.bottom - support(A)
无支撑     → gap = ∞
```

`gap` 的分布：

```
gap 区间            数量   占比
[    0,     1)     575   51.6%   ← 一半构件直接落座上，正确
[    1,     5)       8    0.7%
[    5,    10)      24    2.2%
[   10,    25)     114   10.2%
[   25,    50)      98    8.8%
[   50,   100)      81    7.3%
[  100,   200)      58    5.2%
[  200,   400)      76    6.8%
[  400,     ∞)      71    6.4%
```

**没有断点。** 看具体条目就明白为什么：

```
gap=1490.0  bottom=3800.0  下方支撑面=2310   Roof_Crown          ← 屋脊正中，压在屋顶上
gap=1104.5  bottom=1984.5  下方支撑面= 880   EXT_CorrTurn_B1Roof ← 连廊的屋顶，下面是有柱子的
gap= 632.5  bottom= 632.5  下方支撑面=   0   EXT_StairB_07       ← 台阶踏面 7
gap= 613.5  bottom= 613.5  下方支撑面=   0   EXT_StairB_06       ← 台阶踏面 6
```

- **屋顶压在屋顶上**（重檐）→ 几何上像"悬空"
- **台阶踏面之间几乎不水平重叠** → 每一级都被判"正下方没支撑"
- **装饰件**（`Roof_Crown` 落在屋脊正中的小构件）→ 下方支撑面找偏了

方法 ④ 唯一做对的一件事：**"无支撑"的那 64 个正是地形层本身**（`EXT_Plaza_Central`、`EXT_Water_Bed_*`、`EXT_SideRamp_*`）——它们确实没有东西支撑，因为它们就是底。这个发现有用，但它同时说明"无支撑"不能直接当作"异常"。

### 结论与落地做法

> 与其提供一个 50% 误报率、却"看起来能自动干活"的检测器（那是最危险的假绿），
> 不如把检测限定在语义明确的场合。

落地成三条：

1. **地面从显式参照物取**（`ground_ref` = 某块广场/台基的 label，取它的**顶面**）。没有"估计"，就没有"估计错了"。
2. **可选限定巡检区域**（`region`），只在没有竖向堆叠的地方巡检。
3. **不可信就拒绝动手**：不给参照物时结论挂 `unguided` 警告且 `actionable=False`；占比异常时由覆盖范围检查兜底。

代码：`src/vision/judge.py` 的 `GroundReference` / `find_reference` / `judge_against_ground` / `summarise`；
技能参数：`visual_inspect` 的 `ground_ref` / `region` / `region_pad`。

### 一个顺带撞出来的 bug（安全闸打死了自己）

改成受控模式后第一次跑 Demo2，检测器**正确找到了**浮空的构件，却被自己的安全闸拦下：

```
floating_count: 1, flagged_ratio: 0.3333, reliable: False, actionable: False
reason: 被判浮空的 Actor 占比 33.3% 超过阈值 25%
```

原因：占比上限（25%）原本是用来发现"**地面估错了**"的——一大半东西被判浮空，多半是地面算偏了。但**地面是显式参照物测出来的**，就不存在"估错"，占比高只说明"这块区域里确实有这么多东西浮着"。小区域里 3 个构件、1 个真悬空，33% > 25%，被自己的闸打死。

修法：把三个门槛各管一件事——

| 门槛 | 管什么 | 什么时候启用 |
|---|---|---|
| 分母已知 | 算不准可信度就拒绝 | 总是 |
| 占比上限 | 发现"**地面估错**" | 只在**统计地面**时 |
| 覆盖范围 | 参照物必须真的盖住巡检区域 | 只在**受控模式**时 |

---

<a id="f2"></a>
## F2. 保存是否落盘：只有文件 mtime 可信

三种候选证据，逐个实测：

| 证据 | 结论 | 实测现象 |
|---|---|---|
| 保存 API 返回 | ✗ | 只说明调用没报错 |
| `get_dirty_map_packages()` | ✗ 单用时 | 写入 Actor 后仍返回 `dirty_count: 0` —— **不反映 Python 侧修改** |
| `.umap` 文件 mtime | ✓ | 写入**不**推进，保存**推进** |

实测轨迹：

```
写入 Actor（set_location）后      → dirty_count: 0，.umap mtime 不变
调用 save_current_level 后       → dirty_count: 0，.umap mtime 推进
```

所以"脏包 0 → 0"这种**空证据**被判 `skipped`（不假绿），把 mtime 推进当主证据。

有意思的是保存**真的发生**时，脏包检查反而有用了：复位保存那一次拿到了
`脏包 1 → 0`（非空证据，正常通过）。

代码：`check_file_written`（比较 mtime）、`check_dirty_empty`（带空证据闸门）、
后端方法 `level_file_stat()`。

---

<a id="f3"></a>
## F3. 最严重的一条：弱检查"假红"导致相对位移被重复执行

这是唯一一次**真的把场景改坏**，完整复盘。

### 现场

Demo2 的搬运步骤指定了 HYBRID（视觉留证 + 结构化改值），被建议重排为首选。
实测日志（`logs/runs/demo2b-*.jsonl`）：

```
[2/3] actor_move  {"actor": "EXT_MarkerPost_3_W", "axis": "z", "delta": -400.0, "visual": true}

-> task='actor_move' method=HYBRID
   unverified  1044.8 ms
   verify: executed=4  failures=[ visual.changed: 期望 变化像素占比 >= 0.0005, 实际 0.0 ]
   [通过] transform.location   期望 [-7000,-3200,240]  实际 [-7000,-3200,240]
   [通过] move.delta           期望 'Z 变化 -400.0'    实际 '-400.0000'  Z: 640 → 240
   [通过] others_unchanged     XY 未变

-> task='actor_move' method=UNREAL_MCP      ← 降级重试
   success     665.7 ms
   [通过] move.delta           Z: 240 → -160
```

**发生了什么**：

1. HYBRID 其实**干对了**（640 → 240，三项状态检查全过）；
2. 但一条"整屏前后帧变化"的弱检查判它失败（`visual.changed` = 0.0）；
3. 框架按设计"验证不通过 → 降级"，于是换了 `UNREAL_MCP` **又做了一遍同样的相对位移**；
4. 构件到了 **-160**（多下移 400cm，沉进地面）；
5. 最后一步复验说 `floating_count: 0` —— 因为**沉进地面不是浮空**；
6. 计划报告 **"全部成功"**。

这是本项目最想防的那种失败：接口全绿、验收通过、场景已经坏了。

### 为什么弱检查会假红

事后把两张视口截图拿出来量（`artifacts/move_before_viewport.png` / `move_after_viewport.png`，
960×540，非字节相同，说明渲染确实变了）：

| 处理 | 变化占比 | 最大像素差 | bbox |
|---|---|---|---|
| 全分辨率 | 1.35e-5 | 26 | 146×26 |
| 缩到 960px | 1.35e-5 | 26 | 146×26 |
| 缩到 640px | 4.34e-6 | 12 | 1×1 |
| 缩到 480px | **0.0** | 7 | None |
| 缩到 320px（原设置） | **0.0** | **3** | None |

两个原因叠加：

- **缩放把微弱信号抹平了**：320px 下最大像素差只剩 3，阈值是 12，根本够不到；
- **小且远的物体在整幅画面里本来就只占万分之一**：即使全分辨率，1.35e-5 也低于配置的
  `min_ratio=5e-4`（0.05%）。

所以整屏差分**不能**为"一个远处小构件移动了"当 pass/fail 门槛。

### 三处修复

**① 弱检查降级为 `advisory`**
`check_visual_changed(..., advisory=True)`：低于阈值记 `skipped`（并写明"整屏截图对这种操作不具鉴别力"），不记 `failed`。这与 `level_save` 的处理一致。

**② 不可幂等动作的重试护栏**（更根本）
`Skill.is_idempotent(params)` 声明"重复执行结果是否与执行一次相同"：

```python
# actor_move
def is_idempotent(self, params):
    return params.get("location") is not None   # 绝对定位幂等；相对位移不幂等
```

Executor 的规则：

- **`perform` 抛错之后**（副作用不明）→ 任何动作都先问一句"目标状态现在满足了没有"。
  幂等动作问它是为了避免"目标已达成却报失败"的假红；**不可幂等动作问它是安全要求**
  ——一旦误判成失败，降级重试就会让副作用生效两次。
- **验证不通过之后** → 只有不可幂等动作才需要确认。幂等动作的验证不通过说明目标确实没达成。

**③ 别把代码 bug 记成"通道不健康"**
事故当天还踩了另一个坑：一次 `TypeError`（我自己把 `exclude_labels` 写成了 `ignore_labels`）
被记进通道健康度，连续两次就把 `UNREAL_MCP` 冷却隔离了，之后连只读巡检都选不出任何方式。

`UNEXPECTED`（本地代码异常）**不应该**算作"这个通道不健康"的证据。现在它照样算这次尝试失败，
但**不记账**到健康度与降级预算，并打一条明确的日志说明这是本地缺陷。
另加 `uha stats --reset` 供运维"销账"。

### 专项回归测试

`tests/test_offline.py::test_non_idempotent_retry_guard` 直接模拟这个形状：

| 用例 | 断言 |
|---|---|
| A：写入已落下 + 抛错 | 判**成功**；只写了 **1** 次；状态恰好挪 1 个 delta；**没有**降级重试 |
| B：写入没落下 + 抛错 | 真失败→降级；最终状态**没被多改** |
| B2：降级后成功 | 总共只写入 **1** 次；最终恰好 -400（**不是 -800**） |
| C：幂等动作抛错 | 也会先确认再判失败，最终落在目标值 |
| D：`ActorMoveSkill.is_idempotent` | 相对位移→`False`；绝对定位→`True` |

---

<a id="f4"></a>
## F4. 其它踩过的坑（简记）

| 现象 | 根因 | 处理 |
|---|---|---|
| `actor_inspect` 报"未返回 transform" | `get_transform` 的返回是**顶层单行**，而行抽取只找了嵌套 list/dict | 修正行抽取，识别顶层扁平行 |
| `AttributeError: 'list' object has no attribute 'skipped'` | 调用方写成 `[check_transform_equals(...)]`，多套了一层列表 | 报告装配时把嵌套列表**拉平**；对不认识的东西直接 `TypeError`，不静默忽略 |
| 验证报告"静默全跳过" | verifier 收到的是 `dict` 而不是 `ActorRef`，`getattr` 读了个空 | 加 `_field` / `_vec`，两种形态都能读 |
| `fallback_skip UNREAL_MCP`（第三步被跳过） | 成功也计入 `attempts`，跑满 2 次就"耗尽" | `attempts`（总尝试）与 `failures`（失败）分开计数，每任务 `reset()` |
| `Server already initialized` (HTTP 400) | Router 每步都探可用性，`connect()` / `initialize()` 不幂等，重复握手 | `connect()` / `initialize()` 改成幂等，返回缓存结果 |
| Ctrl+S"成功"但文件 mtime 没变 | Computer Use **没有**"把窗口切到前台"的工具，热键发给了错误的窗口 | `ctrl_s_save` 在 `desktop.editor_focus_point` 未标定时**直接报错**，干净降级到保存 API，而不是盲发热键 |
| GUI 可用性误报为 True | `connect()` 失败返回 `{"available": False}`——非空字典，`bool()` 得 `True` | 读 `available` 字段本身 |
| `save.dirty_cleared` 假绿 | `dirty_count 0→0` 是空证据 | 加空证据闸门 → `skipped`；主证据换成 mtime |

---

<a id="f5"></a>
## F5. 记账缺陷的连锁反应：从"跑一遍测试"到"没有任何可用的执行方式"

F3 修完之后，这篇文档的作者以为"假红 → 重复生效"这条最危险的路已经封死了。然后我跑了一遍离线回归测试，紧接着的真实运行就变成了这样：

```
[1/3] actor_find {'actor': 'Hall_Floor'}
-> task='actor_find' method=HYBRID reason='无规则命中：按各通道固有成本排序'   ← 首选怎么会是 HYBRID？
   error        43.9 ms
-> task='actor_find' method=KEYBOARD / VISION / MOUSE   ← 三个全 error
[ERROR] 计划在第 1 步失败：actor_find: 失败 · 尝试 4 次（含降级）
        错误=未标定 world_outliner 面板的 ROI（vision.roi.world_outliner）…   ← 这个报错和病因毫无关系
```

下一个进程更干脆：

```
[ERROR] 没有可用执行方式：没有任何可用的执行方式
```

**四个问题叠在一起**，每一个单独看都不致命，叠起来就把框架完全锁死了。

### ① 测试写进了生产状态

`tests/test_offline.py` 里几处构造 `Executor(..., config=load_config(), logger=log)` —— `load_config()` 不带参数就是**生产配置**，而 `Executor.__init__` 会据此在 `.state/routing_stats.json` 上建一个 `MethodHealth`。测试里的假通道于是把成功与失败**记进了真实账本**。

账本长这样（注意 `consecutive_failures=0` 却在冷却——这条记录本身就是坏的）：

```json
"methods": {
  "UNREAL_MCP": {"ok": 19, "failed": 2, "consecutive_failures": 0, "cooldown_until": 1789898002.3, "last_error": null},
  "KEYBOARD":   {"ok": 0,  "failed": 2, "consecutive_failures": 2, "cooldown_until": 1789898017.6, "last_error": "NOT_SUPPORTED"},
  "HYBRID":     {"ok": 4,  "failed": 2, "consecutive_failures": 2, "cooldown_until": 1789898017.6, "last_error": "NOT_SUPPORTED"}
}
```

`UNREAL_MCP` 是这台机器上唯一能读写坐标的通道，它被两次测试失败推进了 60 s 冷却 → **下一个真实任务只能在没有结构化通道的情况下选路**。

### ② `NOT_SUPPORTED` 被当成"通道不健康"

被冷却挤走之后，路由只能退到 GUI 通道去做一次**只读查询**。而 HYBRID / KEYBOARD / VISION / MOUSE 本来就不做只读查询，它们全部抛 `NOT_SUPPORTED`。

问题是这个 `NOT_SUPPORTED` 也被记进了健康度——**"通道不擅长这件事"被记成了"通道坏了"**。连续两次之后它们也带上了冷却，于是下一个进程连一个候选都选不出来。

这个缺陷会**自我放大**：

```
优选通道冷却 → 只能退到不匹配的通道 → 它们也全部"不健康"
→ 候选集为空 → 所有任务都失败 → 更多失败入账
```

修法与 `UNEXPECTED` 一致（F3-③）：`NOT_SUPPORTED` 仍然算本次尝试失败（避免原地重试），但**不记健康度、不拉低成功率**，并留下一条 `health_skip` 事件说明这是结构性不匹配：

```
UNEXPECTED     = 我们自己的代码坏了         → 不是通道的问题
NOT_SUPPORTED  = 任务与通道天生不匹配       → 也不是通道的问题
```

### ③ 成功之后没有解除冷却

`MethodHealth.record(ok=True)` 当时只清了 `consecutive_failures` 与 `last_error`，**没碰 `cooldown_until`**。于是留下"连续失败=0 但仍在冷却中"这种自相矛盾的记录——`uha stats` 读起来全是误导，排查时会被带偏到"通道坏了"而不是"账没销"。

冷却的前提是"连续失败 N 次"；既然已经成功，这个前提就不成立了，所以现在成功一次即解除冷却（历史计数 `ok` / `failed` 照旧累计，不清零）。

### ④ 失败信息指向了无关的病因

最后一个尝试是 `MOUSE`，它抱怨的是"world_outliner 的 ROI 没标定"。那是个**真实存在的问题**，但它和"这次任务为什么失败"毫无关系——真正的病因是"最好的通道正在冷却"。

现在 `Executor` 在返回失败前会检查一遍可用性快照里哪些通道正处于冷却，并把它写进错误信息与日志：

```
（注：UNREAL_MCP 正处于冷却期被排除，本次失败可能只是通道健康度问题，
  而不是参数不对；可先 `uha stats` 看账、必要时 `uha stats --reset` 销账）
```

### 四处修复与回归测试

| # | 修法 | 位置 | 回归测试 |
|---|---|---|---|
| ① | 测试用隔离配置（临时目录），并加**收尾断言**盯着生产文件一字未改 | `tests/test_offline.py` | `test_suite_does_not_touch_production_state` |
| ② | `NOT_SUPPORTED` 不进健康度与成功率 | `executor._record_failure` | `test_not_supported_does_not_poison_health` |
| ③ | 成功即解除冷却（记录自洽） | `fallback.MethodHealth.record` | `test_health_record_is_self_consistent` |
| ④ | 失败信息点出冷却中的通道 + `uha stats` 显示剩余冷却时间 | `executor._cooldown_note` / `uha stats` | `test_cooldown_is_surfaced_in_failure` |

修完之后重跑同一套演示，账本是干净的（`HYBRID ok=2 failed=0`、`UNREAL_MCP ok=20 failed=0`，KEYBOARD 那次 `NOT_SUPPORTED` 根本没进账）：

```
[1/3] actor_find  -> UNREAL_MCP  reason="已知 Actor 名称 'Hall_Floor'"        ← 首选回到正确的位置
[3/3] level_save  -> KEYBOARD error(NOT_SUPPORTED) -> UNREAL_MCP success
```

### 一句话总结

**账本错了，选路就会错；而账本错得越隐蔽，"看起来能跑"的假象就越牢固。**
三类失败必须分开对待：

| 失败 | 该记账吗 | 为什么 |
|---|---|---|
| `TOOL_CALL_FAILED` / `VERIFICATION_FAILED`（真失败） | ✅ 记 | 这才是"通道不健康"的证据 |
| `UNEXPECTED`（本地代码缺陷） | ❌ 不记 | 通道没坏，是代码坏了 |
| `NOT_SUPPORTED`（任务与通道不匹配） | ❌ 不记 | 重跑一百次也一样不匹配，记了只会自我放大 |

---

## 可复现性

本文所有数字都能重新跑出来：

```bash
# F1：底边分布与四种方法的误报率
python tools/probes/diag_floating.py     # 分布 + 各分位数
python tools/probes/diag_local.py        # 局部邻域低分位
python tools/probes/diag_support.py      # 物理支撑面 + gap 分布

# F2：保存证据（注意：会触发一次真实保存）
python tools/probes/probe_save_evidence.py

# F3：弱检查的鉴别力（需要 demo2 跑过留下的两张视口截图）
#     见 tests/test_offline.py::test_non_idempotent_retry_guard 的模拟用例

# F5：账本缺陷的完整复现
python tests/test_offline.py     # 内含 4 条对应回归 + 1 条"不污染生产状态"收尾断言
python uha.py stats              # 看账本：冷却中的通道会标出来
python uha.py stats --reset      # 销账

# 回归测试（不需要 UE）
python tests/test_offline.py     # 195 项
python tests/test_router.py      #  24 项
```

> 注：`tools/probes/` 下是诊断脚本，需要一个**正在运行的 UE + MCP**。清单与各脚本负责的问题见
> [`tools/probes/README.md`](../tools/probes/README.md)。它们读的是特定关卡
> `ExamplePalace` 的场景结构，换关卡数字会变、结论（多峰 → 统计地面不可用）不变。
