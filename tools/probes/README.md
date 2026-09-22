> Public edition: project names are anonymized; historical measurements are retained.

# 探针脚本（diagnostic probes）

这些脚本**不是**框架的一部分，是为了回答某个具体问题而临时写的一次性工具。留在这里是因为
`docs/FINDINGS.md` 里每一条实测结论都能靠它们重新跑出来。

## 使用前提

- 需要**一个正在运行的 UE 编辑器 + unreal-mcp 服务**（`python uha.py doctor` 里 `UNREAL_MCP` 必须是 OK）。
- 它们读的是本机关卡 `ExamplePalace` 的场景结构。**换关卡数字会变，但结论不变**
  （例如"底边 Z 是多峰分布 → 统计地面不可用"这件事，任何多层建筑都成立）。
- 除了 `probe_save_evidence.py` 会触发**一次真实保存**，其余都是**只读**的。

## 清单

| 脚本 | 回答什么问题 | 对应发现 |
|---|---|---|
| `diag_floating.py` | 场景的"底边 Z"是什么分布？用不同分位数当"地面"会判出多少浮空？ | [F1](../../docs/FINDINGS.md#f1) |
| `diag_local.py` | 只看局部邻域（最近 N 个邻居）能不能修好"统计地面"？ | [F1](../../docs/FINDINGS.md#f1) |
| `diag_support.py` | 用物理定义（正下方支撑面）判浮空呢？`gap` 分布有没有可切的断点？ | [F1](../../docs/FINDINGS.md#f1) |
| `diag_dirty.py` | 脏包查询到底反不反映 Python 侧的修改？ | [F2](../../docs/FINDINGS.md#f2) |
| `probe_save_evidence.py` | "保存真的发生了"有哪几种候选证据？哪种可信？（**会触发一次保存**） | [F2](../../docs/FINDINGS.md#f2) |
| `diag_fixture.py` | Demo2 该拿哪个构件当试件？地面参照物选哪个？ | [DEMO §1](../../docs/DEMO.md#1-环境与试件) |
| `probe_caps.py` | MCP 服务实际暴露了哪些域与 action？ | [F4](../../docs/FINDINGS.md#f4) |
| `probe_transform.py` | `get_transform` 的返回长什么样？（为什么行抽取会漏） | [F4](../../docs/FINDINGS.md#f4) |
| `probe_shapes.py` | `get_all_details` 的响应有哪几种形态？ | [F4](../../docs/FINDINGS.md#f4) |
| `probe_cu.py` / `probe_cu2.py` / `probe_cu3.py` | Computer Use 的 HTTP 端点为什么拒握手（`Server already initialized`）？ | [F4](../../docs/FINDINGS.md#f4) |

## 运行

```bash
python tools/probes/diag_floating.py
```

脚本自己会把项目根加进 `sys.path`（`Path(__file__).resolve().parents[2]`），所以在任何目录下都能跑。
