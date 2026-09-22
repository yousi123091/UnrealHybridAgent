"""验证器：操作做完之后，怎么**独立地**确认它真的生效了。

"独立"是这个模块的核心价值：
    * 不要用写入操作自己的返回值当证据（``set_location`` 返回 success=true
      只说明"调用没抛异常"，不代表 Actor 真的动了）；
    * 一定要**重新读一遍**状态，拿读回来的值和期望值比。

所以这里的每个 verifier 都接受一个"读取函数"或"读回来的快照"，
而不是接受"写入操作的返回"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..core.errors import BackendUnavailable, ToolCallFailed
from ..vision.image import DiffResult
from .result import CheckResult, VerifyReport

DEFAULT_POS_TOL = 0.5    # cm；UE 单位是厘米，0.5cm 以内视为一致
DEFAULT_ROT_TOL = 0.1    # 度
DEFAULT_SCALE_TOL = 1e-3


def _field(obj: Any, key: str, default: Any = None) -> Any:
    """从"对象或字典"里取字段。

    验证器会被喂两种形态：``ActorRef``（对象）和 ``ActorRef.as_dict()``（字典）。
    早期只用 ``getattr``，于是传进来 dict 时读不到 location，
    检查会**静默变成 skipped**——不是假绿，但等于没验。统一在这里兼容。
    """
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _vec(obj: Any, key: str) -> list[float]:
    raw = _field(obj, key)
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        return [float(raw.get("x", 0.0)), float(raw.get("y", 0.0)), float(raw.get("z", 0.0))]
    try:
        return [float(v) for v in raw]
    except (TypeError, ValueError):
        return []


# --- 单项检查 ----------------------------------------------------------------


def check_actor_found(ref: Any | None, *, label: str) -> CheckResult:
    if ref is None:
        return CheckResult(
            "actor_exists", passed=False, expected=f"存在 {label}", actual=None,
            detail="重新读取时没有找到该 Actor",
        )
    return CheckResult("actor_exists", passed=True, expected=label, actual=_field(ref, "label", label))


def check_transform_equals(
    after: Any,
    *,
    expected_location: Sequence[float] | None = None,
    expected_rotation: Sequence[float] | None = None,
    expected_scale: Sequence[float] | None = None,
    pos_tol: float = DEFAULT_POS_TOL,
    rot_tol: float = DEFAULT_ROT_TOL,
    scale_tol: float = DEFAULT_SCALE_TOL,
    label: str = "",
) -> list[CheckResult]:
    """重新读到的 transform 是否等于期望值。

    ``after`` 可以是 ``ActorRef``，也可以是它的 ``as_dict()`` 结果。
    """
    out: list[CheckResult] = []
    who = label or _field(after, "label", "") or _field(after, "name", "?")

    if expected_location is not None:
        actual = _vec(after, "location")
        if len(actual) < 3:
            out.append(CheckResult(
                "transform.location", passed=False, expected=list(expected_location), actual=actual,
                skipped=True, reason="读取结果里没有 location",
            ))
        else:
            worst = max(abs(float(actual[i]) - float(expected_location[i])) for i in range(3))
            out.append(CheckResult(
                "transform.location",
                passed=worst <= pos_tol,
                expected=[round(float(v), 3) for v in expected_location],
                actual=[round(float(v), 3) for v in actual],
                detail=f"{who} 最大偏差 {worst:.4f}（容差 {pos_tol}）",
            ))

    if expected_rotation is not None:
        actual = _vec(after, "rotation")
        if len(actual) < 3:
            out.append(CheckResult(
                "transform.rotation", passed=False, expected=list(expected_rotation), actual=actual,
                skipped=True, reason="读取结果里没有 rotation",
            ))
        else:
            worst = max(abs(float(actual[i]) - float(expected_rotation[i])) for i in range(3))
            out.append(CheckResult(
                "transform.rotation", passed=worst <= rot_tol,
                expected=[round(float(v), 3) for v in expected_rotation],
                actual=[round(float(v), 3) for v in actual],
                detail=f"最大偏差 {worst:.4f}（容差 {rot_tol}）",
            ))

    if expected_scale is not None:
        actual = _vec(after, "scale")
        if len(actual) < 3:
            out.append(CheckResult(
                "transform.scale", passed=False, expected=list(expected_scale), actual=actual,
                skipped=True, reason="读取结果里没有 scale",
            ))
        else:
            worst = max(abs(float(actual[i]) - float(expected_scale[i])) for i in range(3))
            out.append(CheckResult(
                "transform.scale", passed=worst <= scale_tol,
                expected=[round(float(v), 3) for v in expected_scale],
                actual=[round(float(v), 3) for v in actual],
                detail=f"最大偏差 {worst:.5f}（容差 {scale_tol}）",
            ))

    return out


def check_axis_delta(
    before: Sequence[float],
    after: Sequence[float],
    *,
    axis: int,
    delta: float = 0.0,
    direction: str = "any",
    tol: float = DEFAULT_POS_TOL,
    name: str = "axis_delta",
) -> CheckResult:
    """某一轴的变化量是否符合预期（Demo 1 的 "Z 抬高 20" 就靠它）。"""
    axis_name = "XYZ"[axis] if 0 <= axis < 3 else f"#{axis}"
    if len(before) < 3 or len(after) < 3:
        return CheckResult(name, passed=False, skipped=True, reason="坐标缺分量")
    actual_delta = float(after[axis]) - float(before[axis])

    if delta:
        ok = abs(actual_delta - delta) <= tol
        expected = f"{axis_name} 变化 {delta:+.3f}±{tol}"
    elif direction == "increase":
        ok = actual_delta > tol
        expected = f"{axis_name} 增加（>0）"
    elif direction == "decrease":
        ok = actual_delta < -tol
        expected = f"{axis_name} 减少（<0）"
    else:
        ok = abs(actual_delta) > tol
        expected = f"{axis_name} 有变化"

    return CheckResult(
        name, passed=ok, expected=expected, actual=f"{actual_delta:+.4f}",
        detail=f"{axis_name}: {float(before[axis]):.3f} → {float(after[axis]):.3f}",
    )


def check_unchanged(
    before: Sequence[float],
    after: Sequence[float],
    *,
    axes: Iterable[int] = (0, 1),
    tol: float = DEFAULT_POS_TOL,
    name: str = "others_unchanged",
) -> CheckResult:
    """确认"不该动的轴没被动"——防止"点错 Actor 把旁边的东西也挪了"。"""
    worst = 0.0
    for i in axes:
        if i < len(before) and i < len(after):
            worst = max(worst, abs(float(after[i]) - float(before[i])))
    return CheckResult(
        name, passed=worst <= tol,
        expected=f"未指定轴变化 ≤ {tol}", actual=f"{worst:.4f}",
        detail="轴 " + ",".join("XYZ"[i] for i in axes if i < 3),
    )


def check_save_result(result: Mapping[str, Any] | None) -> CheckResult:
    """保存动作的返回值。

    注意：这里**只能**证明"保存 API 说它保存了"。
    真正的证据是 :func:`check_dirty_empty` —— 保存后重新查脏包列表应为空。
    """
    if not result:
        return CheckResult(
            "save.call_reported", passed=False, skipped=True, reason="保存动作没有返回结果"
        )
    ok = bool(result.get("success", result.get("ok", False)))
    return CheckResult(
        "save.call_reported", passed=ok,
        expected="success=true", actual=result.get("success", result.get("ok")),
        detail=str(result.get("message") or result.get("level_path") or "")[:160],
    )


def check_dirty_empty(dirty_state: Mapping[str, Any] | None, *, before_count: int | None = None) -> CheckResult:
    """保存后重新查脏包：应该变干净。

    **重要（实测踩坑）**：在 UE 5.8 + GenOrca 插件下，
    ``EditorLoadingAndSavingUtils.get_dirty_map_packages()`` **不反映**
    Python 侧通过 ``set_actor_location`` 做的修改 —— 改完 Actor，它照样返回 0。

    所以这里有一道"空证据"闸门：**保存前后都是 0 时结论必须是 skipped**，
    否则就会出现"什么都没发生，却报告保存成功"的假绿。
    真正的保存证据请用 :func:`check_file_written`（比较 .umap 的 mtime）。
    """
    if not dirty_state:
        return CheckResult(
            "save.dirty_cleared", passed=False, skipped=True,
            reason="后端不提供脏包查询（level_dirty_state）",
        )
    count = dirty_state.get("dirty_count")
    if count is None:
        return CheckResult(
            "save.dirty_cleared", passed=False, skipped=True, reason="脏包查询未返回 dirty_count"
        )
    count = int(count)
    if before_count is not None and int(before_count) == 0 and count == 0:
        return CheckResult(
            "save.dirty_cleared", passed=False, skipped=True,
            reason="保存前后脏包都是 0——本机脏包查询不反映 Python 侧修改，该证据无法区分'保存了'与'没保存'",
            actual=0,
        )
    ok = count == 0
    detail = "已无未保存修改"
    if before_count is not None:
        detail = f"脏包 {before_count} → {count}"
    return CheckResult(
        "save.dirty_cleared", passed=ok, expected="dirty_count=0", actual=count, detail=detail,
    )


def check_file_written(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    *,
    name: str = "save.file_written",
) -> CheckResult:
    """**外部证据**：Level 对应的磁盘文件在保存后是否真的被写过。

    依据 mtime 是否推进（mtime_after > mtime_before）。这是进程外的、
    文件系统层面的硬事实——比"保存 API 说它保存了"强得多。

    两个快照都必须带 ``path`` / ``mtime``；缺任何一个都只能 skipped。
    """
    if not before or not after:
        return CheckResult(name, passed=False, skipped=True,
                           reason="缺少保存前后的文件快照（level_file_stat）")
    bp, ap = before.get("path"), after.get("path")
    bm, am = before.get("mtime"), after.get("mtime")
    if bp is None or bm is None or am is None:
        return CheckResult(name, passed=False, skipped=True,
                           reason="文件快照缺少 path/mtime")
    if bp != ap:
        return CheckResult(name, passed=False, skipped=True,
                           reason=f"当前 Level 变了（{bp} -> {ap}），无法比较")
    advanced = float(am) > float(bm) + 1e-6
    return CheckResult(
        name, passed=advanced,
        expected="保存后文件 mtime 推进",
        actual=f"{float(bm):.3f} -> {float(am):.3f}",
        detail=f"{Path(str(bp)).name} 写入时间{'已更新' if advanced else '未变化（保存并未落盘）'}",
    )


def check_visual_changed(
    diff_result: DiffResult | None,
    *,
    min_ratio: float = 0.002,
    name: str = "visual.changed",
    advisory: bool = False,
) -> CheckResult:
    """前后两帧是否有足够变化（纯像素运算，可复现）。

    ``advisory=True`` 用于"画面变化本身不足以定论"的场合（例如整屏截图：
    保存一个 Level 可能完全不改变桌面画面）。这时低于阈值会被判为
    **skipped** 而不是 failed —— 不能因为一个不具鉴别力的指标去否定
    一个已经被硬证据（文件 mtime）证明成功的操作。
    """
    if diff_result is None:
        return CheckResult(name, passed=False, skipped=True, reason="没有可比较的两帧截图")
    ok = diff_result.changed_ratio >= min_ratio
    if not ok and advisory:
        return CheckResult(
            name, passed=False, skipped=True,
            actual=round(diff_result.changed_ratio, 6),
            reason=(f"画面变化占比仅 {diff_result.changed_ratio:.6f} < {min_ratio}，"
                    "整屏截图对这种操作不具鉴别力，不作为失败"),
        )
    return CheckResult(
        name, passed=ok,
        expected=f"变化像素占比 ≥ {min_ratio}",
        actual=round(diff_result.changed_ratio, 6),
        detail=f"变化区域 {diff_result.bbox.as_dict() if diff_result.bbox else None}",
    )


def check_floating_resolved(
    candidates_after: Sequence[Mapping[str, Any]],
    *,
    target_label: str = "",
    name: str = "scene.no_floating",
) -> CheckResult:
    """操作后场景里是否还有浮空 Actor。"""
    labels = [str(c.get("label", "")) for c in candidates_after]
    if target_label:
        ok = target_label not in labels
        return CheckResult(
            name, passed=ok,
            expected=f"{target_label} 不再浮空",
            actual=f"仍浮空: {labels}" if not ok else "已落地",
        )
    ok = not candidates_after
    return CheckResult(
        name, passed=ok, expected="无浮空 Actor",
        actual=f"仍有 {len(candidates_after)} 个: {labels}",
    )


# --- 组装 --------------------------------------------------------------------


class Verifier:
    """把若干读取动作 + 检查串起来，产出 :class:`VerifyReport`。

    用法::

        v = Verifier()
        v.read("actor", lambda: backend.get_actor_transform("Foo"))
        v.check(lambda after: check_transform_equals(after, expected_location=[...]))
        report = v.run()
    """

    def __init__(self) -> None:
        self._readers: list[tuple[str, Callable[[], Any], bool]] = []
        self._smart: list[Callable[[dict[str, Any]], list[CheckResult] | CheckResult]] = []
        self._direct: list[CheckResult] = []
        self._values: dict[str, Any] = {}
        self._errors: dict[str, str] = {}

    # -- 声明 ----------------------------------------------------------------

    def read(self, key: str, fn: Callable[[], Any], *, optional: bool = False) -> "Verifier":
        self._readers.append((key, fn, optional))
        return self

    def check(self, fn: Callable[[dict[str, Any]], list[CheckResult] | CheckResult]) -> "Verifier":
        self._smart.append(fn)
        return self

    def add(self, *checks: CheckResult) -> "Verifier":
        self._direct.extend(checks)
        return self

    def value(self, key: str, value: Any) -> "Verifier":
        self._values[key] = value
        return self

    # -- 执行 ----------------------------------------------------------------

    def run(self) -> VerifyReport:
        report = VerifyReport()
        report.extend(self._direct)

        for key, fn, optional in self._readers:
            try:
                self._values[key] = fn()
            except (ToolCallFailed, BackendUnavailable) as exc:
                self._errors[key] = f"{type(exc).__name__}: {exc}"
                if not optional:
                    report.add(CheckResult(
                        f"read.{key}", passed=False, skipped=True,
                        reason=f"读取失败：{self._errors[key]}",
                    ))

        for fn in self._smart:
            try:
                result = fn(self._values)
            except Exception as exc:  # noqa: BLE001 - 验证器自己出错不能吞掉
                report.add(CheckResult(
                    "verify.internal", passed=False, skipped=True,
                    reason=f"验证器异常：{type(exc).__name__}: {exc}",
                ))
                continue
            if isinstance(result, CheckResult):
                report.add(result)
            else:
                report.extend(list(result))

        return report


def as_list(result: CheckResult | list[CheckResult]) -> list[CheckResult]:
    return [result] if isinstance(result, CheckResult) else list(result)


__all__ = [
    "DEFAULT_POS_TOL",
    "DEFAULT_ROT_TOL",
    "DEFAULT_SCALE_TOL",
    "Verifier",
    "check_actor_found",
    "check_transform_equals",
    "check_axis_delta",
    "check_unchanged",
    "check_save_result",
    "check_dirty_empty",
    "check_file_written",
    "check_visual_changed",
    "check_floating_resolved",
    "as_list",
]
