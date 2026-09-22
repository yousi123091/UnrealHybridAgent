> Public edition: project names are anonymized; historical measurements are retained.

# 演示：两个端到端场景

这份文档是**照着跑一遍**的说明书，不是宣传材料。下面贴的输出都是真机跑出来的原文（含"验证跳过"和"降级重试"那些不好看的部分）。

两个演示各自想证明一件事：

| 演示 | 一句话 | 证明什么 |
|---|---|---|
| **Demo1** | 抬高一个已知构件并保存 Level | 结构化通道的**写入 + 复验**闭环，以及保存到底有没有落盘的**真证据** |
| **Demo2** | 视觉发现浮空 → 混合通道修正 → 视觉复验 | **不可信就拒绝动手**，以及"弱检查不许把好动作判死"（见 [`FINDINGS.md#f3`](FINDINGS.md#f3)） |

---

## 1. 环境与试件

```bash
python uha.py doctor     # 先确认通道在线，别在离线环境上试
```

期望看到（本机）：

```
=== 通道可用性 ===
  OK   UNREAL_MCP          ← 结构化：读写坐标、保存、视口截图
  DOWN UE_PYTHON           ← 需要编辑器开启 bRemoteExecution
  DOWN UE_COMMANDLET       ← 与"已打开的编辑器会话"互斥
  OK   MOUSE / KEYBOARD / VISION / HYBRID   ← GUI/视觉：Computer Use 在线即可
```

演示用关卡：`ExamplePalace`（示例宫殿风格场景，1184 个 Actor）。

**试件（实测坐标，单位 cm）**：

| label | location | 包围盒 extent | 用途 |
|---|---|---|---|
| `Hall_Floor` | `[7800, 0, 840]` | `[2700, 4500, 20]` | Demo1 的搬运对象（一块位于台面上的地板） |
| `EXT_Plaza_Central` | 广场地形，顶面 **Z=100** | 覆盖 `[-18000,-5000,-10000,10000]` | Demo2 的**显式地面参照物** |
| `EXT_MarkerPost_3_W` | `[-7000, -3200, 240]` | `[40, 40, 140]`（scale 0.8/0.8/2.8） | Demo2 的浮空试件；底边 = 240-140 = **100** = 广场顶面 |

Demo2 的巡检区域取 `[-7500,-6500,-3700,-2700]`——一块**只有 3 个构件**的开放广场（没有竖向堆叠），这是能安全巡检的前提。

> ⚠️ 两个演示都会**真的改关卡**。跑完请照 §4 复位。

---

## 2. Demo1：抬高地坪并保存

```bash
python uha.py demo1 --actor Hall_Floor --delta 20
```

计划（`uha plan --demo1 --actor Hall_Floor --delta 20` 可以先只看不跑）：

```
计划 demo1_raise_and_save（3 步）：把「Hall_Floor」的 Z 提高 20，然后保存 Level
  1. actor_find  — 找到 Actor「Hall_Floor」并读取当前变换
  2. actor_move  — 把 Z 轴坐标提高 20
  3. level_save  — 保存当前 Level（Ctrl+S）
```

### 实际输出（耗时 46.7 s）

```
[1/3] actor_find {'actor': 'Hall_Floor'}
-> task='actor_find' method=UNREAL_MCP reason="已知 Actor 名称 'Hall_Floor'：结构化通道可直接寻址，无需 GUI 搜索"
   success     551.6 ms  verify={'passed': True, 'executed': 1, 'skipped': 0, 'failures': [],
                                  'checks': [{'name': 'actor_exists', 'passed': True}]}

[2/3] actor_move {'actor': 'Hall_Floor', 'axis': 'z', 'delta': 20.0}
-> task='actor_move' method=UNREAL_MCP reason='需要精确数值（坐标/旋转/缩放），只有结构化通道能保证'
   success    1137.3 ms  verify={'passed': True, 'executed': 3, 'skipped': 0, 'failures': [],
                                  'checks': [{'name': 'transform.location', 'passed': True,
                                              'expected': [7800.0, 0.0, 860.0], 'actual': [7800.0, 0.0, 860.0]},
                                             {'name': 'move.delta', 'passed': True,
                                              'detail': 'Z: 840.000 → 860.000'},
                                             {'name': 'others_unchanged', 'passed': True,
                                              'detail': '轴 X,Y', 'actual': '0.0000'}]}

[3/3] level_save {}
-> task='level_save' method=KEYBOARD reason="已知快捷键 'ctrl s' 且单步可完成（~2 步），无需截图"
   error      1801.0 ms          ← NOT_SUPPORTED：focus point 未标定，拒绝盲发热键
-> task='level_save' method=UNREAL_MCP reason='UNREAL_MCP: 上一次方式失败后的降级尝试'
   success    4277.9 ms  verify={'passed': True, 'executed': 3, 'skipped': 1, 'failures': [],
                                  'checks': [{'name': 'save.call_reported', 'passed': True, 'detail': 'Saved current level.'},
                                             {'name': 'visual.screen_changed', 'passed': True, 'actual': 0.036281},
                                             {'name': 'save.file_written', 'passed': True,
                                              'detail': 'ExamplePalace.umap 写入时间已更新',
                                              'actual': '1789897444.898 -> 1789898315.968'},
                                             {'name': 'save.dirty_cleared', 'passed': False, 'skipped': True,
                                              'reason': '保存前后脏包都是 0——本机脏包查询不反映 Python 侧修改，该证据无法区分"保存了"与"没保存"'}])

计划 demo1_raise_and_save：全部成功
  1. [OK] actor_find: 成功 · 方式=UNREAL_MCP · 验证通过（1/1 项通过）
  2. [OK] actor_move: 成功 · 方式=UNREAL_MCP · 验证通过（3/3 项通过）
  3. [OK] level_save: 成功 · 方式=UNREAL_MCP · 尝试 2 次（含降级）· 验证通过（3/3 项通过，1 项跳过）
```

### 三件值得注意的事

**① 第 3 步走了两遍，这是设计行为，不是缺陷。**
`rule_save_shortcut` 把 Ctrl+S 排在前面（它是 UE 的既定交互，1 步完成）。但本机 `desktop.editor_focus_point` 没标定，Computer Use **没有**"把窗口切到前台"的工具——热键会发给当前焦点窗口，而那个窗口不一定是编辑器。所以 `ctrl_s_save` 直接抛 `NOT_SUPPORTED` 干净降级，而不是盲发一次可能发错窗口的热键。

> 想看它一步到位：标定 `desktop.editor_focus_point`（见 [`ROADMAP.md`](ROADMAP.md) §6）。

**② 保存的"真证据"是 `.umap` 的 mtime。**
`save.call_reported: True` 只说明调用没抛异常；`save.dirty_cleared` 是 `skipped`——脏包 `0 → 0` 是**空证据**（本机 `get_dirty_map_packages()` 不反映 Python 侧修改），按规矩记"没验成"而不是"通过"。真正定案的是 `1789897444.898 -> 1789898315.968`。详见 [`FINDINGS.md#f2`](FINDINGS.md#f2)。

**③ 46.7 s 里大部分不是干活。**
每一步都要新建 MCP 会话，**13~14 s 的连接开销**压在第一个调用上（`[1/3]` 到路由决策之间那段空白）。真正的执行是几百毫秒到几秒。

### 独立复核（别信框架自己说"成功"）

```bash
python tools/observe.py Hall_Floor
```

这个脚本**不经过 UHA 的任何封装**，直接问 MCP 原始通道：

```json
{"transform": {"location": [7800.0, 0.0, 860.0], "scale": [54.0, 90.0, 0.4]},
 "level": {"level_name": "ExamplePalace"},
 "dirty": {"dirty_count": 0}}
```

「抬高了 20」这件事，到这里才算有独立证据。

---

## 3. Demo2：视觉发现浮空 → 混合通道修正 → 视觉复验

### 为什么必须给 `--ground-ref`

先跑一次**不给**参照物的巡检，看它怎么拒绝：

```bash
python uha.py run --skill visual_inspect --set check_floating=true
```

真实输出（这个例子比任何解释都有说服力）：

```
visual_inspect: 成功 · 方式=UNREAL_MCP · 验证通过（1/1 项通过，2 项跳过）
   [通过] inspect.evidence_captured —— 通过（期望 '至少一张截图' / 实际 ['ue_viewport']）
   [跳过] inspect.verdict —— 被判浮空的 Actor 占比 94.7% 超过阈值 25%——这通常说明
          **地面高度估错了**，而不是真有这么多东西浮着。结论仅作参考，**不会**据此自动修改场景。
          （地面是统计推断的（未指定地面参照物）。多层建筑里统计地面不可靠：实测全局中位数误报
            48%、低分位误报 95%。建议传 ground_ref 指定一块地板/广场。）
   [跳过] inspect.vlm_channel —— Agent-TARS vlmConfigured=false，语义视觉定位不可用
```

关键在返回的判定体里（`--json` 看得到）：

```json
{"floating_count": 1117, "flagged_ratio": 0.9474, "basis": "statistical_low_percentile",
 "reliable": false, "actionable": false, "unguided": true}
```

**它在这里判出 1117 / 1179 个"浮空"（94.7%）** —— 和 [`FINDINGS.md#f1`](FINDINGS.md#f1) 里用低分位估计地面的实测误报率**一模一样**，因为整个场景本来就没有东西浮空。框架没有把这个数字当成结论，而是判 `actionable: false` 并**拒绝据此自动修改场景**；计划路径下，搬运步骤会因此被前置条件拦住。

这不是保守，是被实测逼出来的：四种"自动估计地面"的做法在真实建筑场景里误报率 **48% / 95% / 77% / 49%**（[`FINDINGS.md#f1`](FINDINGS.md#f1)）。地面从**显式参照物**取（取它的顶面），没有"估计"，就没有"估计错了"。

### 步骤 A：制造一个悬空（演示用）

```bash
python uha.py run --skill actor_move --actor EXT_MarkerPost_3_W --delta 400
# 构件 Z: 240 → 640，底边从 100 抬到 500 —— 离地 400，这就是"浮空"
```

### 步骤 B：跑 Demo2

```bash
python uha.py demo2 --ground-ref EXT_Plaza_Central --region="-7500,-6500,-3700,-2700" --delta 400
```

### 实际输出（耗时 38.7 s）

```
计划 demo2_floating_repair（3 步）：视觉发现浮空 -> 混合通道修正 -> 视觉复验
  1. visual_inspect — 截图巡检，判断场景里有没有 Actor 浮空（地面参照物：EXT_Plaza_Central）
  2. actor_move — 把最明显的浮空 Actor 沿 Z 轴下移 400（HYBRID：动手前后各留一张视口截图 + 走结构化通道精确改值） [建议方式：HYBRID]
  3. visual_inspect — 再截图复验，确认异常已消除

[1/3] visual_inspect {'check_floating': True, 'ground_tolerance': 5.0,
                      'ground_ref': 'EXT_Plaza_Central', 'region': [-7500.0, -6500.0, -3700.0, -2700.0]}
-> task='visual_inspect' method=UNREAL_MCP reason='只读查询：结构化通道无副作用、零步骤开销'
   success     739.8 ms  verify={'passed': True, 'executed': 2, 'skipped': 1, 'failures': [],
      'checks': [
        {'name': 'inspect.evidence_captured', 'passed': True, 'actual': ['ue_viewport']},
        {'name': 'inspect.verdict', 'passed': True,
         'actual': {'floating_count': 1, 'flagged_ratio': 0.3333, 'basis': 'explicit_ground',
                    'reliable': True, 'actionable': True},
         'detail': '地面=100.0（参照物 EXT_Plaza_Central）；疑似浮空 1/3 个（EXT_MarkerPost_3_W）'},
        {'name': 'inspect.vlm_channel', 'passed': True, 'skipped': True,
         'reason': 'Agent-TARS vlmConfigured=false，语义视觉定位不可用'}]}

[2/3] actor_move {'actor': 'EXT_MarkerPost_3_W', 'axis': 'z', 'delta': -400.0, 'visual': True}
-> task='actor_move' method=HYBRID reason='步骤显式建议 HYBRID（Router 本来会选 UNREAL_MCP）'
   success    1759.6 ms  verify={'passed': True, 'executed': 3, 'skipped': 1, 'failures': [],
      'checks': [
        {'name': 'visual.changed', 'passed': False, 'skipped': True, 'actual': 0.0,
         'reason': '画面变化占比仅 0.000000 < 0.0005，整屏截图对这种操作不具鉴别力，不作为失败'},
        {'name': 'transform.location', 'passed': True, 'expected': [-7000.0, -3200.0, 240.0],
         'actual': [-7000.0, -3200.0, 240.0]},
        {'name': 'move.delta', 'passed': True, 'detail': 'Z: 640.000 → 240.000', 'actual': '-400.0000'},
        {'name': 'others_unchanged', 'passed': True, 'detail': '轴 X,Y'}]}

[3/3] visual_inspect {'check_floating': True, 'ground_tolerance': 5.0, 'ground_ref': 'EXT_Plaza_Central',
                      'region': [-7500.0, -6500.0, -3700.0, -2700.0], 'expect_no_floating': True}
-> task='visual_inspect' method=UNREAL_MCP
   success     624.7 ms  verify={'passed': True, 'executed': 3, 'skipped': 1, 'failures': [],
      'checks': [
        {'name': 'inspect.verdict', 'passed': True,
         'actual': {'floating_count': 0, 'flagged_ratio': 0.0, 'basis': 'explicit_ground',
                    'reliable': True, 'actionable': True},
         'detail': '地面=100.0（参照物 EXT_Plaza_Central）；疑似浮空 0/3 个'},
        {'name': 'inspect.no_floating_remaining', 'passed': True, 'expected': 0, 'actual': 0,
         'detail': '巡检范围内已无浮空构件'}]}

计划 demo2_floating_repair：全部成功
  1. [OK] visual_inspect: 成功 · 方式=UNREAL_MCP · 验证通过（2/2 项通过，1 项跳过）
  2. [OK] actor_move:     成功 · 方式=HYBRID    · 验证通过（3/3 项通过，1 项跳过）
  3. [OK] visual_inspect: 成功 · 方式=UNREAL_MCP · 验证通过（3/3 项通过，1 项跳过）
```

### 这次和"改坏场景那一次"的差别

同样的三步、同样的构件、同样的参数，但**行为完全不同**——这正是修 F3 的意义。对比 [`FINDINGS.md#f3`](FINDINGS.md#f3) 的事故现场：

| | 事故那一次（F3） | 现在 |
|---|---|---|
| `visual.changed` 判失败 | 记 **failed** → 触发降级 | 记 **skipped**（advisory）→ 不触发降级 |
| 第 2 步实际执行次数 | **2 次**（HYBRID 一次 + 降级 UNREAL_MCP 又一次） | **1 次** |
| 构件最终 Z | **-160**（沉进地面，多下了 400） | **240**（正好落回广场顶面） |
| 计划报告 | "全部成功" | "全部成功"（这次是真的） |

`visual.changed` 那一行仍然出现 `passed: False` + `actual: 0.0`——**它不是被删掉了，而是被降级为"旁证"**：整屏 960×540 的截图里，一盏 80cm 的柱子动 400cm，变化占比只有 1.35e-5，缩到 320px 宽后连阈值都够不到。弱检查可以提意见，但不能当判决。

### 关于"下移 400"这件事

`drop_by` 是**调用方给的近似值**，不是框架算出来的"精确落地高度"——精确落地需要射线检测 / 包围盒对齐，属第二版（[`ROADMAP.md`](ROADMAP.md) §2）。计划描述里也照实写了"下移 400"，没把它说成"落回地面"。

之所以这次正好停在 240（= 广场顶面 + 包围盒半高 140），是因为**我先把它抬高了 400**，所以压回 400 刚好复原。换个悬空高度，`drop_by` 就要重新给。

---

## 4. 复位（把场景还原）

两个演示都会改关卡，跑完请还原。用**绝对坐标**（绝对定位是幂等的，重复执行不会重复生效——这一点在 [`FINDINGS.md#f3`](FINDINGS.md#f3) 里被证明很重要）：

```bash
# Demo1
python uha.py run --skill actor_move --actor Hall_Floor --location=7800,0,840
python uha.py run --skill level_save

# Demo2
python uha.py run --skill actor_move --actor EXT_MarkerPost_3_W --location=-7000,-3200,240
python uha.py run --skill level_save
```

最后独立复核（这一步别省）：

```bash
python tools/observe.py Hall_Floor                 # 期望 Z=840
python tools/observe.py EXT_MarkerPost_3_W         # 期望 Z=240
```

实测复位结果：

```
Hall_Floor          location [7800.0, 0.0, 840.0]   ✓
EXT_MarkerPost_3_W  location [-7000.0, -3200.0, 240.0] ✓
dirty_count: 0      ← 已保存落盘
```

---

## 5. 不需要 UE 的部分

没有 UE 环境也能跑的部分占了绝大多数逻辑：

```bash
python tests/test_offline.py     # 195 项：MCP 参数映射、几何判定、三态验证、降级记账、幂等护栏、测试隔离
python tests/test_router.py      #  24 项：路由规则与成本模型（含"环境变化会改变决策"）
```

`test_offline.py` 里最有价值的几条是**回归**性质的——每一条都对应一次真实踩坑：浮空判定在多峰场景下的误报、`get_transform` 的顶层单行返回、脏包空证据不许判通过、`UNEXPECTED` / `NOT_SUPPORTED` 不许污染通道健康度、相对位移被判失败后不许重复生效、测试不许写生产状态。

---

## 6. 一次运行怎么复盘

每次运行一份 `logs/runs/<run-id>.jsonl`，逐事件：

```jsonc
{"kind": "routing", ...}          // 完整决策：选中谁、什么理由、淘汰了谁、可用性快照
{"kind": "method_preference", ...} // 步骤级建议：reordered / already_selected / ignored_not_available
{"kind": "step", ...}              // 每一步尝试：参数、结果、验证报告、耗时、错误码
{"kind": "fallback", ...}          // 降级：从哪条通道换到哪条
{"kind": "health_skip", ...}       // 结构化失败被排除出健康度（NOT_SUPPORTED / UNEXPECTED）
{"kind": "method_exhausted", ...}  // 结构性失败：这条路不会再变好
{"kind": "plan_start", ...}        // 整个计划的步骤清单
```

配合 `uha stats` 看跨任务账本。**"框架说成功"不算证据，"独立观测器读出来的状态"才算**——这是这个项目唯一贯穿始终的硬规矩。

---

## 7. 故障排查（都是真踩过的）

### 症状：`没有任何可用的执行方式`，或者报出来的错和实际病因无关

先看账本：

```bash
python uha.py stats
```

```
HYBRID      ok=4 failed=2 连续失败=2 ← 冷却中（剩 43s）
KEYBOARD    ok=0 failed=2 连续失败=2 ← 冷却中（剩 43s）
UNREAL_MCP  ok=19 failed=2 连续失败=0 last_error=None
```

两种情况：

1. **`连续失败=2` 且冷却中**：这些通道近期真失败过，冷却期（60 s）内会被跳过。等一会儿，或者确认失败原因已经修掉之后销账：
   ```bash
   python uha.py stats --reset
   ```
2. **`连续失败=0` 却仍显示冷却**：这是修复前的旧账本形态（成功时没有一并解除冷却，留下一笔自相矛盾的记录）。**现在已经修掉**——成功一次即解除冷却，见 [`FINDINGS.md#f5`](FINDINGS.md#f5)。旧账本用 `--reset` 清一次即可。

另外：失败信息里如果出现「（注：UNREAL_MCP 正处于冷却期被排除…）」，那是框架在提示"**真正的病因可能是通道健康度，而不是本步骤参数不对**"——别再往参数上查了。

### 症状：`未标定 world_outliner 面板的 ROI`

GUI 降级链路需要显式坐标，而 `vision.roi.*` 全是 `None`。这是**设计行为**（宁可明确失败，也不盲点窗口）。

标定步骤：`python uha.py roi-shot` 拍一张全屏截图，对着图把四个面板的 `[x, y, w, h]` 填进 `config/agent.config.json` 的 `vision.roi`。注意分辨率或系统缩放一变就得重标（本机 2560×1600 / scaleFactor 1.4997，逻辑分辨率 1707×1067）。

### 症状：`level_save` 每次都先失败一次再降级

同上，`desktop.editor_focus_point` 没标定 → Ctrl+S 路径拒绝执行（`NOT_SUPPORTED`）→ 降级到保存 API。功能不受影响，只是多花约 2 s。标定那个点之后就会一步到位。

### 症状：跑完离线测试之后的第一次真实运行变得很奇怪

修复前会这样：离线测试里的假通道把失败记进了**生产健康度文件**，于是下一个真实任务的 `UNREAL_MCP` 带着冷却出场。现在已经通过"测试状态隔离 + 收尾断言"堵住了（`test_suite_does_not_touch_production_state`），完整复盘见 [`FINDINGS.md#f5`](FINDINGS.md#f5)。
