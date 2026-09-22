"""UE 侧 Python 脚本片段库。

这个文件里所有字符串都**运行在 Unreal Editor 进程内**，不是在 UHA 进程里。
统一约定：

    * 脚本把结果写进 `__UHA_RESULT__` 字典（成功 `ok=True`，失败 `ok=False`）
    * 脚本把该字典 **同时**写成 JSON 到 `__UHA_OUT_PATH__` 文件，并打印带标记的一行
    * UHA 侧优先读文件（不受日志抓取影响），读不到再退回解析 stdout

为什么用"写文件"而不是只靠 print：UE 的 `run_command` 返回的 output 里混着
引擎自己的日志，靠正则从里面捞 JSON 很脆。落文件是确定性的。

UE API 选择（UE 5.8）：
    * `unreal.EditorActorSubsystem`      —— Actor 增删查（EditorLevelLibrary 已废弃）
    * `unreal.LevelEditorSubsystem`      —— save_current_level
    * `unreal.EditorLoadingAndSavingUtils` —— 查 dirty packages（验证保存是否真的生效）
"""

from __future__ import annotations

import json
import textwrap
from typing import Any

RESULT_VAR = "__UHA_RESULT__"
OUT_PATH_VAR = "__UHA_OUT_PATH__"
BEGIN_MARK = "UHA_JSON_BEGIN"
END_MARK = "UHA_JSON_END"

#: 所有脚本共用的前言：定义写结果与收尾的辅助函数。
PRELUDE = f"""
import unreal, json, traceback, sys

{BEGIN_MARK} = None
{OUT_PATH_VAR} = globals().get({OUT_PATH_VAR!r}, None)
{RESULT_VAR} = {{"ok": True}}

def __uha_vec(v):
    return [float(v.x), float(v.y), float(v.z)]

def __uha_actor_dict(a):
    try:
        loc = a.get_actor_location()
        rot = a.get_actor_rotation()
        scl = a.get_actor_scale3d()
    except Exception:
        loc = rot = scl = None
    def v(x):
        return [float(x.x), float(x.y), float(x.z)] if x is not None else [0.0, 0.0, 0.0]
    return {{
        "name": a.get_name(),
        "label": a.get_actor_label(),
        "class_name": a.get_class().get_name(),
        "path": a.get_path_name(),
        "location": v(loc),
        "rotation": [float(rot.roll), float(rot.pitch), float(rot.yaw)] if rot is not None else [0.0, 0.0, 0.0],
        "scale": v(scl),
    }}
"""

#: 所有脚本共用的尾声：把结果落盘 + 打印标记行。
EPILOGUE = f"""
try:
    if {OUT_PATH_VAR}:
        with open({OUT_PATH_VAR}, "w", encoding="utf-8") as __f:
            json.dump({RESULT_VAR}, __f, ensure_ascii=False, default=str)
except Exception as __e:
    print("UHA_WRITE_FAILED", __e)
print("{BEGIN_MARK}" + json.dumps({RESULT_VAR}, ensure_ascii=False, default=str) + "{END_MARK}")
"""


def _wrap(body: str) -> str:
    return textwrap.dedent(PRELUDE) + textwrap.dedent(body) + textwrap.dedent(EPILOGUE)


def _fail_guard(body: str) -> str:
    """把 body 包进 try/except，保证任何异常都能变成结构化失败结果。"""
    indented = textwrap.indent(textwrap.dedent(body), "    ")
    return f"""
try:
{indented}
except Exception as __e:
    import traceback as __tb
    {RESULT_VAR} = {{"ok": False, "error": str(__e), "traceback": __tb.format_exc(), "stage": "ue_script"}}
"""


# --- op 实现 ------------------------------------------------------------------


def script_get_actors(*, name_like: str | None = None, class_like: str | None = None, limit: int = 500) -> str:
    filters = []
    if name_like:
        filters.append(f"name_like = {name_like!r}")
    if class_like:
        filters.append(f"class_like = {class_like!r}")
    filter_block = "\n".join(filters) if filters else "name_like = None\nclass_like = None"

    body = f"""
{filter_block}

sub = unreal.EditorActorSubsystem()
actors = sub.get_all_level_actors() if sub else []
out = []
for a in actors:
    if a is None:
        continue
    d = __uha_actor_dict(a)
    if name_like and name_like.lower() not in (d["name"] + d["label"]).lower():
        continue
    if class_like and class_like.lower() not in d["class_name"].lower():
        continue
    out.append(d)
    if len(out) >= {int(limit)}:
        break
{RESULT_VAR} = {{"ok": True, "count": len(out), "total": len(actors), "actors": out}}
"""
    return _wrap(_fail_guard(body))


def script_find_actor(name: str) -> str:
    body = f"""
target = {name!r}
sub = unreal.EditorActorSubsystem()
actors = sub.get_all_level_actors() if sub else []

def __score(d):
    n, l = d["name"], d["label"]
    if n == target or l == target:
        return 3
    if n.lower() == target.lower() or l.lower() == target.lower():
        return 2
    if target.lower() in n.lower() or target.lower() in l.lower():
        return 1
    return 0

best = None
best_score = 0
for a in actors:
    if a is None:
        continue
    d = __uha_actor_dict(a)
    s = __score(d)
    if s > best_score:
        best_score, best = s, d

if best is None:
    candidates = [__uha_actor_dict(a)["name"] for a in actors if a is not None][:40]
    {RESULT_VAR} = {{"ok": False, "code": "ACTOR_NOT_FOUND", "error": "未找到 Actor: " + target, "candidates": candidates}}
else:
    {RESULT_VAR} = {{"ok": True, "match_score": best_score, "actor": best}}
"""
    return _wrap(_fail_guard(body))


def script_get_actor_transform(name: str) -> str:
    body = f"""
target = {name!r}
sub = unreal.EditorActorSubsystem()
actors = sub.get_all_level_actors() if sub else []
found = None
for a in actors:
    if a is None:
        continue
    if a.get_name() == target or a.get_actor_label() == target:
        found = a
        break
if found is None:
    for a in actors:
        if a is None:
            continue
        n = a.get_name()
        if target.lower() == n.lower() or target.lower() in n.lower():
            found = a
            break
if found is None:
    {RESULT_VAR} = {{"ok": False, "code": "ACTOR_NOT_FOUND", "error": "未找到 Actor: " + target}}
else:
    {RESULT_VAR} = {{"ok": True, "actor": __uha_actor_dict(found)}}
"""
    return _wrap(_fail_guard(body))


def _setter_script(name: str, op: str) -> str:
    """生成 setter 脚本。约定 `__UHA_ARGS__` 展开为 `(a, b, c)`：

    * location / scale -> (x, y, z)
    * rotation         -> (roll, pitch, yaw)
    """
    if op == "set_actor_location":
        ctor = "new_vec = unreal.Vector(x, y, z)"
        apply = "found.set_actor_location(new_vec, False, False)"
    elif op == "set_actor_rotation":
        ctor = "new_vec = unreal.Rotator(x, y, z)"
        apply = "found.set_actor_rotation(new_vec, False)"
    elif op == "set_actor_scale":
        ctor = "new_vec = unreal.Vector(x, y, z)"
        apply = "found.set_actor_scale3d(new_vec)"
    else:  # pragma: no cover
        raise ValueError(op)

    body = f"""
target = {name!r}
x, y, z = __UHA_ARGS__

sub = unreal.EditorActorSubsystem()
actors = sub.get_all_level_actors() if sub else []
found = None
for a in actors:
    if a is None:
        continue
    if a.get_name() == target or a.get_actor_label() == target:
        found = a
        break
if found is None:
    for a in actors:
        if a is None:
            continue
        if target.lower() in a.get_name().lower():
            found = a
            break

if found is None:
    {RESULT_VAR} = {{"ok": False, "code": "ACTOR_NOT_FOUND", "error": "未找到 Actor: " + target}}
else:
    before = __uha_actor_dict(found)
    {ctor}
    {apply}
    after = __uha_actor_dict(found)
    {RESULT_VAR} = {{"ok": True, "op": {op!r}, "actor": found.get_name(),
                    "before": before, "after": after}}
"""
    return _wrap(_fail_guard(body))


def script_set_actor_location(name: str) -> str:
    return _setter_script(name, "set_actor_location")


def script_set_actor_rotation(name: str) -> str:
    return _setter_script(name, "set_actor_rotation")


def script_set_actor_scale(name: str) -> str:
    return _setter_script(name, "set_actor_scale")


def script_save_level() -> str:
    body = """
les = unreal.LevelEditorSubsystem()
before = __uha_dirty()
try:
    ok = les.save_current_level()
except Exception as __e:
    ok = False
    save_error = str(__e)
else:
    save_error = None
after = __uha_dirty()
if save_error:
    __UHA_RESULT__ = {"ok": False, "code": "SAVE_FAILED", "error": save_error, "level_before": before, "level_after": after}
else:
    __UHA_RESULT__ = {"ok": bool(ok), "level_before": before, "level_after": after,
                      "still_dirty": after.get("dirty_count", 0) > 0}
"""
    # 需要 __uha_dirty 辅助函数
    helper = """
def __uha_dirty():
    info = {"dirty_maps": [], "dirty_content": [], "dirty_count": 0, "map_dirty": False}
    try:
        els = unreal.EditorLoadingAndSavingUtils
        maps = [str(p.get_name()) for p in els.get_dirty_map_packages()]
        content = [str(p.get_name()) for p in els.get_dirty_content_packages()]
        info["dirty_maps"] = maps
        info["dirty_content"] = content
        info["dirty_count"] = len(maps) + len(content)
        info["map_dirty"] = len(maps) > 0
    except Exception as __e:
        info["error"] = str(__e)
    return info

def __uha_current_level():
    try:
        world = unreal.EditorLevelLibrary.get_editor_world() if hasattr(unreal, "EditorLevelLibrary") else None
    except Exception:
        world = None
    try:
        if world is None:
            world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        return str(world.get_path_name())
    except Exception as __e:
        return None
"""
    return _wrap(_fail_guard(helper + body))


def script_level_dirty_state() -> str:
    body = """
def __uha_dirty():
    info = {"dirty_maps": [], "dirty_content": [], "dirty_count": 0, "map_dirty": False}
    try:
        els = unreal.EditorLoadingAndSavingUtils
        info["dirty_maps"] = [str(p.get_name()) for p in els.get_dirty_map_packages()]
        info["dirty_content"] = [str(p.get_name()) for p in els.get_dirty_content_packages()]
        info["dirty_count"] = len(info["dirty_maps"]) + len(info["dirty_content"])
        info["map_dirty"] = len(info["dirty_maps"]) > 0
    except Exception as __e:
        info["error"] = str(__e)
    return info

__UHA_RESULT__ = {"ok": True, **__uha_dirty()}
"""
    return _wrap(_fail_guard(body))


def script_current_level() -> str:
    body = """
path = None
try:
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    path = str(world.get_path_name())
except Exception as __e:
    path = None
    __err = str(__e)
__UHA_RESULT__ = {"ok": path is not None, "level": path}
"""
    return _wrap(_fail_guard(body))


def script_viewport_screenshot(path: str, width: int = 1280, height: int = 720) -> str:
    """UE 内部抓视口截图（结构化，不受桌面遮挡影响）。写 PNG 到 path。"""
    body = f"""
out_path = {path!r}
w, h = {int(width)}, {int(height)}
ok = False
err = None
try:
    sub = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    world = sub.get_editor_world()
    # UE 5.8: EditorLevelLibrary 已移除，改用 automation/game viewport 抓帧
    editors = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    ok = False
    err = "UE 内部视口抓帧在 5.8 需要 EditorViewportSubsystem，当前实现退化为桌面截图"
except Exception as __e:
    err = str(__e)
__UHA_RESULT__ = {{"ok": ok, "path": out_path, "error": err}}
"""
    return _wrap(_fail_guard(body))


def script_execute_python(code: str) -> str:
    """逃生舱：执行任意 UE Python，把局部变量 __UHA_RESULT__ 当作返回值（若用户设置了）。"""
    indented = textwrap.indent(textwrap.dedent(code), "    ")
    body = f"""
{indented}
if {RESULT_VAR} == {{"ok": True}}:
    {RESULT_VAR} = {{"ok": True, "executed": True}}
"""
    return _wrap(_fail_guard(body))


def with_args(script: str, args: tuple[float, float, float] | None = None) -> str:
    """把数值参数注入脚本（避免拼接进脚本字符串导致格式问题）。"""
    if args is None:
        return script.replace("__UHA_ARGS__", "(0.0, 0.0, 0.0)")
    return script.replace("__UHA_ARGS__", f"({float(args[0])}, {float(args[1])}, {float(args[2])})")


BUILDERS: dict[str, Any] = {
    "get_actors": script_get_actors,
    "find_actor": script_find_actor,
    "get_actor_transform": script_get_actor_transform,
    "set_actor_location": script_set_actor_location,
    "set_actor_rotation": script_set_actor_rotation,
    "set_actor_scale": script_set_actor_scale,
    "save_level": script_save_level,
    "level_dirty_state": script_level_dirty_state,
    "current_level": script_current_level,
    "execute_ue_python": script_execute_python,
}


def parse_result(output: str) -> dict[str, Any] | None:
    """从 run_command 的 output 文本里捞出 JSON（落盘失败时的兜底）。"""
    if not output:
        return None
    start = output.find(BEGIN_MARK)
    end = output.find(END_MARK)
    if start < 0 or end < 0 or end <= start:
        return None
    raw = output[start + len(BEGIN_MARK) : end]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


__all__ = [
    "BUILDERS",
    "RESULT_VAR",
    "OUT_PATH_VAR",
    "BEGIN_MARK",
    "END_MARK",
    "parse_result",
    "with_args",
    "script_get_actors",
    "script_find_actor",
    "script_get_actor_transform",
    "script_set_actor_location",
    "script_set_actor_rotation",
    "script_set_actor_scale",
    "script_save_level",
    "script_level_dirty_state",
    "script_current_level",
    "script_execute_python",
    "script_viewport_screenshot",
    "MINIMAL_JSON_DUMP",
]

#: 留给 commandlet 模式用的极简落盘片段（供外壳脚本追加）
MINIMAL_JSON_DUMP = EPILOGUE
