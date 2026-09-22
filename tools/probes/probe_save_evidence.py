"""探针 4：找一个**可靠**的"保存是否真的发生"证据。

已知问题：``EditorLoadingAndSavingUtils.get_dirty_map_packages()`` 在本机
（UE 5.8 + 该插件）**不反映** Python 侧的 Actor 修改 —— 写完之后它照样返回 0。
所以拿它当"保存成功"的证据是**假绿**。

本探针对比三种候选证据：
  A. 磁盘上 .umap 文件的 mtime（最外部、最可信）
  B. 各种 dirty 查询 API 是否还有别的可用
  C. util.save_all_dirty 的返回值
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # tools/probes/ -> 项目根

from src.adapters.mcp_client import make_client_from_config  # noqa: E402
from src.core.config import load_config  # noqa: E402

SURVEY = """
import unreal, json as _json
out = {}
# 1) EditorLoadingAndSavingUtils 上所有带 dirty 的函数
els = unreal.EditorLoadingAndSavingUtils
out["els_dirty_funcs"] = sorted(n for n in dir(els) if "dirty" in n.lower())
# 2) PackageTools / EditorAssetLibrary / 其它可能有用的
for cls_name in ("PackageTools", "EditorAssetLibrary", "EditorUtilityLibrary", "EditorLevelUtils"):
    cls = getattr(unreal, cls_name, None)
    if cls is not None:
        out[cls_name + "_dirty"] = sorted(n for n in dir(cls) if "dirty" in n.lower() or "save" in n.lower())
# 3) 当前世界 -> 包 -> 文件路径
world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
pkg = world.get_outermost() if world else None
out["world_path"] = str(world.get_path_name()) if world else None
out["content_dir"] = unreal.Paths.project_content_dir()
out["project_dir"] = unreal.Paths.project_dir()
out["converted"] = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir())
# 4) 包是否 dirty（对象级）
try:
    out["pkg_is_dirty"] = bool(pkg.is_dirty()) if pkg else None
except Exception as e:
    out["pkg_is_dirty_error"] = str(e)
print(_json.dumps(out, default=str))
"""

MTIME = """
import unreal, json as _json, os, glob
content = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir())
hits = []
for pat in ("**/*.umap",):
    hits += glob.glob(os.path.join(content, pat), recursive=True)
info = []
for h in hits[:20]:
    try:
        info.append({"file": h, "mtime": os.path.getmtime(h), "size": os.path.getsize(h)})
    except Exception as e:
        info.append({"file": h, "error": str(e)})
print(_json.dumps({"content_dir": content, "maps": info}, default=str))
"""


def main() -> int:
    cfg = load_config()
    c = make_client_from_config(cfg.section("mcp_servers.unreal_mcp"), name="uha-probe4")
    c.initialize()
    c.tool_names(refresh=True)

    def py(code):
        res = c.call_tool("util", {"action": "execute_python", "params": {"code": code}})
        d = res.get("data") or {}
        raw = d.get("result")
        try:
            return json.loads(raw) if isinstance(raw, str) else d
        except json.JSONDecodeError:
            return {"raw": raw}

    def call(domain, action, params=None):
        res = c.call_tool(domain, {"action": action, "params": params or {}})
        return res.get("data") if isinstance(res, dict) else res

    print("=" * 72)
    print("A) 可用的 dirty / save API 调查")
    print(json.dumps(py(SURVEY), ensure_ascii=False, indent=2)[:2500])

    print("=" * 72)
    print("B) .umap 文件 mtime 基线")
    print(json.dumps(py(MTIME), ensure_ascii=False, indent=2)[:1500])

    print("=" * 72)
    print("C) 写一次(+20) -> 看 dirty 与 mtime 是否变化")
    call("actor", "set_location", {"actor_label": "Hall_Floor", "location": [7800.0, 0.0, 860.0]})
    print("  写后 dirty:", json.dumps(py("""
import unreal, json as _json
pkgs = unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages()
print(_json.dumps({"dirty_count": len(pkgs)}))
"""), ensure_ascii=False))
    print("  写后 mtime:", json.dumps(py(MTIME), ensure_ascii=False)[:600])

    print("=" * 72)
    print("D) 保存 -> 看 mtime 是否推进")
    print("  save ->", call("level", "save_current_level"))
    print("  保存后 mtime:", json.dumps(py(MTIME), ensure_ascii=False)[:600])

    print("=" * 72)
    print("E) 复原坐标 [7800,0,840] 并保存")
    call("actor", "set_location", {"actor_label": "Hall_Floor", "location": [7800.0, 0.0, 840.0]})
    print("  save ->", call("level", "save_current_level"))
    print("  最终:", json.dumps(call("actor", "get_transform", {"actor_label": "Hall_Floor"}), ensure_ascii=False))

    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
