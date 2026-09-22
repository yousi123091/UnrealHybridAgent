#!/usr/bin/env python
"""UnrealHybridAgent CLI。

    python uha.py doctor                 环境自检（通道是否都在线）
    python uha.py list                   列出可用 skill
    python uha.py plan  "把 XX 抬高 20"   只出计划，不执行（先看再做）
    python uha.py run   --skill actor_move --actor XX --axis z --delta 20
    python uha.py demo1 --actor XX        MVP 演示 1：抬高 + 保存 + 复验
    python uha.py demo2                   MVP 演示 2：视觉发现浮空 + 修正 + 复验
    python uha.py stats                  查看方法健康度（哪种方式最可靠）
    python uha.py roi-shot               拍一张标定用截图

约定：**能先看就先看**。`plan` 只输出计划，`--dry-run` 只走选路不真执行。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_THIS = Path(__file__).resolve().parent
if str(_THIS) not in sys.path:
    sys.path.insert(0, str(_THIS))

from src.core.config import load_config                      # noqa: E402
from src.core.log import RunLogger                           # noqa: E402
from src.planner import Planner, plan_from_text              # noqa: E402
from src.runtime import build_bundle                         # noqa: E402
from src.scheduler import Executor                           # noqa: E402
from src.skills.registry import SKILLS, describe_all, get_skill  # noqa: E402


def _setup(args: argparse.Namespace) -> tuple[object, RunLogger, Executor]:
    cfg = load_config(args.config, reload=True)
    if getattr(args, "dry_run", False):
        cfg.data.setdefault("desktop", {})["dry_run"] = True
    mode = getattr(args, "mode", None)
    if mode:
        cfg.data.setdefault("execution", {})["mode"] = str(mode).upper()
    log = RunLogger(
        cfg.path("logs", ensure_parent=True),
        run_name=getattr(args, "run_name", None) or "cli",
        console=not getattr(args, "quiet", False),
        level=str(cfg.get("logging.level", "INFO")),
    )

    # --- UAH presence layer：装配阶段（可选、旁路、失败不影响主流程）---------
    # 这一段只做一件事：把"UHA 正在连通道"这个**真实**的启动阶段报给 UAH。
    # 它不读写任何 UHA 状态，也不会因为 UAH 出问题而影响下面的装配。
    uah_boot = None
    try:
        from uah.hosts.embedded.bootstrap import uah_begin_boot

        uah_boot = uah_begin_boot(cfg, log)
    except Exception as exc:  # noqa: BLE001 - UAH 故障绝不影响 UHA
        log.event("uah_boot_error", error=str(exc)[:200])

    bundle = build_bundle(cfg)
    executor = Executor(bundle, cfg, log)
    if mode:
        executor.set_execution_mode(str(mode).upper())
    if getattr(args, "approve", False):
        executor.approval._confirmer = lambda req: True  # type: ignore[attr-defined]
        executor.approval.granted_by_default = "cli_approve"  # type: ignore[attr-defined]

    # --- P0 Safety: independent SafetyController + Banner + Global EStop hotkey ---
    safety = None
    banner = None
    hotkey = None
    if not getattr(args, "dry_run", False):
        try:
            from src.desktop.hotkey import EmergencyHotkey, release_all_keys_and_buttons
            from src.safety.banner import build_safety_banner
            from src.safety.controller import get_safety_controller
            from src.core.session_control import get_controller
            from src.desktop.overlay import ControlOverlay

            controller = get_controller()
            hotkey_spec = str(cfg.get("desktop.emergency_hotkey") or "ctrl+alt+f12")
            safety = get_safety_controller(hotkey=hotkey_spec)

            def _safety_estop() -> None:
                release_all_keys_and_buttons()
                try:
                    if bundle.computer_use is not None:
                        bundle.computer_use.release_control(force=True)
                except Exception:
                    pass
                try:
                    executor.coordinator.force_release_all()
                except Exception:
                    pass
                try:
                    controller.emergency_stop(reason="safety_estop")
                except Exception:
                    pass

            safety._on_estop.append(_safety_estop)  # explicit registration

            hotkey = EmergencyHotkey(
                lambda: safety.emergency_stop(reason="global_hotkey", source="uha_hotkey"),
                hotkey=hotkey_spec,
            )
            hotkey.start()
            safety.set_hotkey_registered(bool(hotkey.registered or hotkey.mode == "polling"))

            if bool(cfg.get("desktop.safety_banner.enabled", True)):
                banner = build_safety_banner(
                    safety,
                    geometry=str((cfg.get("desktop.safety_banner") or {}).get("geometry") or "1280x72+40+8"),
                )
                # Declare availability BEFORE any visibility claim (P0.1 §7 banner-first).
                safety.set_banner_available(bool(banner.available))
                banner.start()
                if banner.visible:
                    safety.banner_visible(ok=True)
                log.event(
                    "safety_banner",
                    backend=getattr(banner, "backend", "tkinter"),
                    available=bool(banner.available),
                    visible=bool(banner.visible),
                )

            sc = safety.startup_selfcheck()
            if not sc.get("computer_use_allowed", True):
                # Fail closed: disable Computer Use
                if bundle.computer_use is not None:
                    try:
                        bundle.computer_use.close()
                    except Exception:
                        pass
                    bundle.computer_use = None
                log.event("computer_use_disabled_safety_selfcheck", detail=sc)
            else:
                log.event(
                    "safety_layer_ready",
                    hotkey=hotkey_spec,
                    hotkey_mode=hotkey.mode,
                    banner=bool(banner and banner.available),
                    selfcheck=sc,
                )

            # Optional small overlay (task HUD) — never replaces safety banner
            if cfg.get("desktop.overlay.enabled", False):
                ov_cfg = cfg.get("desktop.overlay") or {}
                overlay = ControlOverlay(
                    controller,
                    geometry=str(ov_cfg.get("geometry") or "320x180+20+20"),
                )
                overlay.start()
                executor._overlay = overlay  # type: ignore[attr-defined]
            executor._hotkey = hotkey  # type: ignore[attr-defined]
            executor._safety = safety  # type: ignore[attr-defined]
            executor._safety_banner = banner  # type: ignore[attr-defined]

            # P0.1 §14: on process teardown, unhook the observer and release ONLY the
            # keys/buttons UHA itself pressed — never the user's.
            import atexit

            def _exit_cleanup() -> None:
                try:
                    from src.safety.cleanup import force_cleanup_input

                    force_cleanup_input(safety, computer_use=bundle.computer_use)
                except Exception:
                    pass

            atexit.register(_exit_cleanup)
        except Exception as exc:  # noqa: BLE001
            log.event("safety_layer_error", error=str(exc)[:200])
            # Fail closed on safety init failure
            if bundle.computer_use is not None:
                try:
                    bundle.computer_use.close()
                except Exception:
                    pass
                bundle.computer_use = None

    # --- UAH presence layer：挂观察者（可选、旁路、行为零改动）-------------
    # 包裹 executor.run_plan / run 与 approval.evaluate：**只观察**，
    # 返回值原样透传、异常原样抛出，UHA 的行为与"UAH 是否存在"完全无关。
    # 详细边界见 uah/adapters/uha/adapter.py 顶部说明。
    try:
        from uah.hosts.embedded.bootstrap import uah_attach

        uah_attach(cfg, log, executor, boot=uah_boot)
    except Exception as exc:  # noqa: BLE001 - UAH 故障绝不影响 UHA
        log.event("uah_attach_error", error=str(exc)[:200])

    return bundle, log, executor


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _auto_type(value: str) -> object:
    """`--set k=1` 里的 1 应该是数字而不是字符串。"""
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null", ""):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    if value.startswith("[") and value.endswith("]"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value


# --- 子命令 -------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    """分层环境自检：区分进程存在 / 插件挂载 / TCP 监听 / 真实请求成功。"""
    from tools.phase3_doctor import collect_doctor, format_report

    report = collect_doctor(args.config)
    print(format_report(report))

    # 附加：skill 列表与 legacy availability（便于对照）
    try:
        from src.skills.registry import describe_all as _describe_all

        print("\n=== Skills ===")
        print("  " + ", ".join(s["name"] for s in _describe_all()))
    except Exception as exc:  # noqa: BLE001
        print(f"\nSkills 列表失败: {exc}")

    if args.json:
        _print_json(report)
    summary = report.get("summary") or {}
    # 真实 MCP 闭环未成功则 doctor 非零，避免假绿
    return 0 if summary.get("mcp_request_ok") else 2


def cmd_list(args: argparse.Namespace) -> int:
    for info in describe_all():
        print(f"{info['name']:<16} {info['description']}")
        print(f"{'':<16} 关键词: {', '.join(info['keywords']) or '-'}")
        print(f"{'':<16} 倾向方式: {', '.join(info['preferred_methods']) or '-'}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    planner = Planner()
    if args.demo1:
        plan = planner.plan_raise_and_save(args.actor or "", float(args.delta), axis=args.axis)
    elif args.demo2:
        plan = planner.plan_floating_repair(
            drop_by=float(args.delta or 50.0),
            ground_ref=args.ground_ref,
            region=_parse_region(args.region),
            region_pad=(float(args.region_pad) if args.region_pad is not None else None),
            precise=bool(getattr(args, "precise", False)),
        )
    else:
        plan = plan_from_text(args.text or "", params={"actor": args.actor, "axis": args.axis, "delta": args.delta})
    print(plan.describe())
    if args.json:
        _print_json(plan.as_dict())
    return 0


def _run_skill(args: argparse.Namespace) -> int:
    _, log, executor = _setup(args)
    skill = get_skill(args.skill)
    params: dict[str, object] = {}
    if args.actor:
        params["actor"] = args.actor
    if args.axis:
        params["axis"] = args.axis
    if args.delta is not None:
        params["delta"] = float(args.delta)
    if args.location:
        params["location"] = [float(v) for v in args.location.split(",")]
    if args.save:
        params["save"] = True
    if args.visual:
        params["visual"] = True
    for extra in args.set or []:
        key, _, value = extra.partition("=")
        params[key.strip()] = _auto_type(value.strip())

    result = executor.run(skill, params)
    print(result.summary())
    if result.verification:
        for check in result.verification.checks:
            print("   " + check.line())
    if args.json:
        _print_json(result.as_dict())
    return 0 if result.ok else 2


def _run_demo(args: argparse.Namespace, plan: object) -> int:
    _, log, executor = _setup(args)
    result = executor.run_plan(plan)
    print(result.describe())
    print(f"\n耗时 {result.duration_ms:.0f} ms")
    print(f"日志：{log.jsonl.path}")
    print(f"产物目录：{executor.artifacts_dir}")
    try:
        print("Router 质量：", json.dumps(executor.quality.summary(), ensure_ascii=False)[:400])
        print("锁统计：", json.dumps(executor.coordinator.stats(), ensure_ascii=False)[:300])
    except Exception:
        pass
    # 收起浮窗 / 停热键
    ov = getattr(executor, "_overlay", None)
    if ov is not None:
        try:
            ov.stop()
        except Exception:
            pass
    hk = getattr(executor, "_hotkey", None)
    if hk is not None:
        try:
            hk.stop()
        except Exception:
            pass
    artifacts = sorted(p for p in Path(executor.artifacts_dir).glob("**/*") if p.is_file())
    if artifacts:
        print("本次产物：")
        for p in artifacts[-8:]:
            print(f"   {p}")
    if args.json:
        _print_json(result.as_dict())
    return 0 if result.ok else 2


def cmd_demo1(args: argparse.Namespace) -> int:
    if not args.actor:
        print("demo1 需要 --actor 指定一个已知 Actor 的名字", file=sys.stderr)
        return 1
    delta = float(args.delta) if args.delta is not None else 20.0
    plan = Planner().plan_raise_and_save(args.actor, delta, axis=args.axis)
    return _run_demo(args, plan)


def cmd_demo2(args: argparse.Namespace) -> int:
    precise = bool(getattr(args, "precise", False))
    plan = Planner().plan_floating_repair(
        drop_by=float(args.delta or 50.0),
        ground_ref=args.ground_ref,
        region=_parse_region(args.region),
        region_pad=(float(args.region_pad) if args.region_pad is not None else None),
        precise=precise,
    )
    return _run_demo(args, plan)


def _parse_region(raw: str | None) -> list[float] | None:
    """``--region x0,x1,y0,y1`` -> ``[x0, x1, y0, y1]``。"""
    if not raw:
        return None
    parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise SystemExit("--region 需要 4 个数字：x0,x1,y0,y1")
    return [float(p) for p in parts]


def cmd_plan_run(args: argparse.Namespace) -> int:
    plan = plan_from_text(args.text or "", params={"actor": args.actor, "axis": args.axis, "delta": args.delta})
    if args.plan_only:
        print(plan.describe())
        return 0
    return _run_demo(args, plan)


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    from src.router.fallback import MethodHealth
    from src.router.quality import RouterQualityTracker

    health = MethodHealth(cfg.path("router.stats_file"))
    quality = RouterQualityTracker(cfg.path("router.quality_file") if cfg.get("router.quality_file") else
                                   cfg.path("router.stats_file").with_name("router_quality.json"))
    if getattr(args, "reset", False):
        health.clear()
        if getattr(args, "all", False):
            quality.clear()
        print("已清空方法健康度记录（通道冷却一并解除）"
              + ("；Router 质量账本一并清空" if getattr(args, "all", False) else ""))
        return 0
    stats = health.stats()
    now = time.time()
    print("=== 方法健康度（支持 task_category::METHOD） ===")
    if not stats:
        print("  还没有记录")
    cooling: list[str] = []
    for method, slot in sorted(stats.items()):
        until = float(slot.get("cooldown_until") or 0.0)
        state = ""
        if until > now:
            cooling.append(method)
            state = f"  ← 冷却中（剩 {until - now:.0f}s）"
        print(f"  {method:<28} ok={slot.get('ok')} failed={slot.get('failed')} "
              f"连续失败={slot.get('consecutive_failures')} last_error={slot.get('last_error')}{state}")
    if cooling:
        print(f"\n注意：{', '.join(cooling)} 正在冷却期，期间的选路会跳过它们。")
        print("      如果这些失败来自本地代码缺陷或「任务与通道不匹配」，账本就该销掉：")
        print("      python uha.py stats --reset")
    print("\n=== Router 选路质量（first-choice / fallback / regret） ===")
    qs = quality.summary()
    if not qs:
        print("  还没有记录")
    for cat, row in sorted(qs.items()):
        print(f"  {cat}: tasks={row['tasks']} "
              f"first_choice={row['first_choice_success_rate']} "
              f"fallback_rate={row['fallback_rate']} "
              f"avg_attempts={row['average_attempts']} "
              f"regret={row['method_regret']}")
    if args.json:
        _print_json({"health": stats, "quality": qs})
    return 0


def cmd_roi_shot(args: argparse.Namespace) -> int:
    _, log, executor = _setup(args)
    from src.skills.gui_helpers import desktop_screenshot
    from src.skills.base import SkillContext

    ctx = SkillContext(
        bundle=executor.bundle, config=executor.config, logger=log,
        artifacts_dir=executor.artifacts_dir,
    )
    shot, path = desktop_screenshot(ctx, "roi_calibration.png")
    print(f"已保存标定截图：{path}")
    print(f"逻辑分辨率 {shot.width}x{shot.height}（scale_factor={shot.scale_factor}）")
    print("把 World Outliner / Details 面板的 [x, y, w, h] 填进 config 的 vision.roi 即可启用 GUI 降级链路")
    return 0


# --- 参数表 -------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    """公共选项。

    同时挂在主 parser 与每个子 parser 上（用 parents 继承），
    这样 ``uha --actor X run ...`` 和 ``uha run ... --actor X`` **两种写法都能用**。
    命令行工具在这一点上不该有脾气。
    """
    p.add_argument("--config", default=None, help="配置文件路径（默认 config/agent.config.json）")
    p.add_argument("--json", action="store_true", help="额外输出 JSON")
    p.add_argument("--quiet", action="store_true", help="不打印到控制台")
    p.add_argument("--dry-run", action="store_true", help="只选路不真执行")
    p.add_argument("--run-name", default=None, help="本次运行的日志名")
    p.add_argument("--actor", default=None, help="目标 Actor 名（Level 里的 label）")
    p.add_argument("--axis", default="z", choices=["x", "y", "z"], help="轴向，默认 z")
    p.add_argument("--delta", default=None, help="相对位移（厘米，UE 世界单位）")
    p.add_argument("--location", default=None, help="绝对坐标 x,y,z")
    p.add_argument("--save", action="store_true", help="动作后顺带保存 Level")
    p.add_argument("--visual", action="store_true", help="要求视觉留证（会倾向 HYBRID）")
    p.add_argument("--set", action="append", help="附加参数 key=value，可重复")
    p.add_argument("--text", default=None, help="自然语言任务描述")
    p.add_argument("--ground-ref", default=None,
                   help="地面参照物 label（demo2 用；指定后浮空判定才可执行）")
    p.add_argument("--region", default=None,
                   help="巡检区域 x0,x1,y0,y1（世界坐标，cm；不给则全场景）")
    p.add_argument("--region-pad", default=None,
                   help="以 ground-ref 的水平范围外扩这么多 cm 作为巡检区域")
    p.add_argument("--precise", action="store_true",
                   help="demo2：精确落回地面（line_trace + 绝对坐标），替代近似 drop_by")
    p.add_argument("--mode", default=None, choices=["auto", "confirm", "AUTO", "CONFIRM"],
                   help="执行模式：auto=全托管 / confirm=人工确认（默认读配置 execution.mode=CONFIRM）")
    p.add_argument("--approve", action="store_true",
                   help="CONFIRM 模式下自动批准（演示/脚本用；仍走 ApprovalGate 业务判定）")
    p.add_argument("--targets", default=None, help="batch_mutation：逗号分隔 Actor label")
    p.add_argument("--task-id", default=None, help="rollback 用的 checkpoint task id")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common)

    p = argparse.ArgumentParser(prog="uha", description="UnrealHybridAgent —— UE5 混合自动化运行时")
    _add_common(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    def make(name: str, help_text: str, **kw: Any) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, parents=[common], **kw)

    make("doctor", "环境自检").set_defaults(func=cmd_doctor)
    make("providers", "Provider 生命周期、能力与健康诊断").set_defaults(func=cmd_providers)
    make("list", "列出 skill").set_defaults(func=cmd_list)

    sp = make("plan", "只出计划")
    sp.add_argument("text", nargs="?", default="")
    sp.add_argument("--demo1", action="store_true")
    sp.add_argument("--demo2", action="store_true")
    sp.set_defaults(func=cmd_plan)

    sr = make("run", "执行单个 skill")
    sr.add_argument("--skill", required=True, choices=sorted(SKILLS))
    sr.set_defaults(func=_run_skill)

    make("demo1", "演示1：抬高已知 Actor 并保存").set_defaults(func=cmd_demo1)
    make("demo2", "演示2：视觉发现浮空并修正").set_defaults(func=cmd_demo2)

    sp3 = make("run-text", "自然语言 -> 计划 -> 执行")
    sp3.add_argument("text", nargs="?", default="")
    sp3.add_argument("--plan-only", action="store_true", help="只看计划不执行")
    sp3.set_defaults(func=cmd_plan_run)

    make("stats", "方法健康度 + Router 选路质量").set_defaults(func=cmd_stats)
    sub.choices["stats"].add_argument("--reset", action="store_true",
                                      help="清空健康度记录（解除通道冷却）")
    sub.choices["stats"].add_argument("--all", action="store_true",
                                      help="连同 Router 质量账本一起清空")
    make("roi-shot", "拍 GUI 标定截图").set_defaults(func=cmd_roi_shot)
    sc = make("calibrate", "Session GUI 标定（自动找 UE 窗口）")
    sc.add_argument("--force", action="store_true", help="强制重新完整标定")
    sc.add_argument("--validate", action="store_true", help="只做低成本布局验证")
    sc.set_defaults(func=cmd_calibrate)
    make("quality", "Router 选路质量统计").set_defaults(func=cmd_quality)
    saf = make("safety", "安全层诊断 / 急停清理 / 显式恢复")
    saf.add_argument("--status", action="store_true", default=True)
    saf.add_argument("--force-cleanup", action="store_true")
    saf.add_argument(
        "--resume",
        action="store_true",
        help="显式恢复 Computer Use（唯一能离开 EMERGENCY_STOP / HUMAN_OVERRIDE 的动作，P0.1 §10）",
    )
    saf.add_argument(
        "--release-everything",
        action="store_true",
        help="人工逃生舱：无差别抬起全部常见按键（会打断用户正在进行的拖拽/按键，仅人工诊断用）",
    )
    saf.set_defaults(func=cmd_safety)
    br = make("batch", "批量修改 Actor（绝对/相对）")
    # --targets/--axis/--delta already on common parser
    br.add_argument("--absolute-z", type=float, default=None)
    br.set_defaults(func=cmd_batch)
    rb = make("rollback", "按 task-id 绝对坐标回滚 checkpoint")
    rb.add_argument("task_id")
    rb.set_defaults(func=cmd_rollback)
    make("checkpoints", "列出 checkpoint").set_defaults(func=cmd_checkpoints)
    cp = make("capabilities", "能力缓存/Provider 状态")
    cp.add_argument("--refresh", action="store_true")
    cp.set_defaults(func=cmd_capabilities)
    ob = make("observe", "任务域观察包（Phase4B）")
    ob.add_argument("--intent", default="prepare_batch_movement")
    ob.add_argument("--delta-observe", action="store_true")
    ob.set_defaults(func=cmd_observe)
    return p


def cmd_observe(args: argparse.Namespace) -> int:
    _, log, executor = _setup(args)
    from src.observation.task_observer import TaskObserver
    from src.semantics.profile import load_profile

    backend = executor.bundle.unreal.get("UNREAL_MCP")
    if backend is None or not backend.available():
        print("UNREAL_MCP unavailable", file=sys.stderr)
        executor.bundle.close()
        return 2
    cfg = load_config(args.config)
    profile = load_profile(cfg.get("semantics.profile") or "example_palace")
    obs = TaskObserver(backend, task_id=f"cli-{int(time.time())}", profile=profile)
    targets = [x.strip() for x in str(args.targets or "").split(",") if x.strip()]
    if args.delta_observe:
        pkg, delta = obs.observe_delta_safe(targets=targets, intent=args.intent)
        _print_json({"package": pkg.as_dict(), "delta": delta.as_dict(), "stats": pkg.stats})
    else:
        pkg = obs.observe(targets=targets, intent=args.intent)
        _print_json({"package": pkg.as_dict(), "stats": pkg.stats})
    executor.bundle.close()
    return 0


def cmd_safety(args: argparse.Namespace) -> int:
    from src.safety.cleanup import force_cleanup_input
    from src.safety.controller import get_safety_controller
    from src.safety.diagnostics import collect_safety_status, format_safety_status

    if getattr(args, "release_everything", False):
        from src.desktop.hotkey import release_everything_now

        print(json.dumps(
            release_everything_now(explicit=True), ensure_ascii=False, indent=2, default=str,
        ))

    if getattr(args, "resume", False):
        from src.safety import controller as controller_module
        if controller_module._CONTROLLER is None:
            print("Resume must be requested on the running Safety Banner; this diagnostic process cannot resume another controller.")
            return 2
        sc = controller_module._CONTROLLER
        before = sc.state.value
        snap = sc.resume_safety(explicit=True)
        print(json.dumps(
            {"resume": "explicit", "state_before": before, "state_after": snap.state.value,
             "allow_input": snap.allow_input,
             "note": "Computer Use stays disabled until the agent requests control again (P0.1 §11)"},
            ensure_ascii=False, indent=2,
        ))

    if getattr(args, "force_cleanup", False):
        out = force_cleanup_input()
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    status = collect_safety_status()
    print(format_safety_status(status))
    if args.json:
        _print_json(status)
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    _, log, executor = _setup(args)
    targets = [x.strip() for x in str(args.targets).split(",") if x.strip()]
    params: dict[str, Any] = {"targets": targets, "mode": (args.mode or "confirm")}
    if args.absolute_z is not None:
        params["absolute"] = True
        params["locations"] = {t: [None, None, float(args.absolute_z)] for t in targets}
        # fill x,y from backend at perform time via absolute single z: use delta path instead
        params["absolute"] = False
        params["axis"] = args.axis
        # compute absolute via per-actor target in script tool; CLI uses delta
        print("提示：--absolute-z 请用 tools/phase4_batch.py 以获取 before 后写绝对坐标", file=sys.stderr)
    if args.delta is not None:
        params["axis"] = args.axis
        params["delta"] = float(args.delta)
    result = executor.run(get_skill("batch_mutation"), params)
    print(result.summary())
    if args.json:
        _print_json(result.as_dict())
    bundle_close = getattr(executor, "bundle", None)
    if bundle_close is not None:
        try:
            bundle_close.close()
        except Exception:
            pass
    return 0 if result.ok else 2


def cmd_rollback(args: argparse.Namespace) -> int:
    cfg = load_config(args.config, reload=True)
    bundle = build_bundle(cfg)
    from src.core.checkpoint import CheckpointStore, rollback_task

    backend = bundle.unreal.get("UNREAL_MCP")
    if backend is None or not backend.available():
        print("UNREAL_MCP unavailable", file=sys.stderr)
        bundle.close()
        return 2
    store = CheckpointStore(cfg.path("workspace.state_dir") / "checkpoints")
    out = rollback_task(backend, store, args.task_id)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    bundle.close()
    return 0 if out.get("ok") else 2


def cmd_checkpoints(args: argparse.Namespace) -> int:
    cfg = load_config(args.config, reload=True)
    from src.core.checkpoint import CheckpointStore

    store = CheckpointStore(cfg.path("workspace.state_dir") / "checkpoints")
    for tid in store.list_ids():
        try:
            data = store.load(tid)
            print(f"{tid}  status={data.get('status')} actors={len(data.get('actors') or [])} mode={data.get('execution_mode')}")
        except Exception as exc:
            print(f"{tid}  ERROR {exc}")
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    bundle = build_bundle(load_config(args.config))
    try:
        report = bundle.gateway.doctor()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if any(name in report["healthy_candidates"] for name in ("UNREAL_MCP", "UE_PYTHON")) else 2
    finally:
        bundle.close()


def cmd_capabilities(args: argparse.Namespace) -> int:
    _, log, executor = _setup(args)
    snap = executor.capability_cache.get(
        lambda: executor.probe(use_cache=False),
        force=bool(args.refresh),
    )
    payload = {
        "snapshot": snap.as_dict(),
        "cache_stats": executor.capability_cache.stats(),
        "execution_mode": executor.approval.mode.value,
        "providers": load_config(args.config).get("providers") or {},
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    executor.bundle.close()
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    from src.desktop.calibration import SessionGuiCalibrator

    cfg = load_config(args.config, reload=True)
    cache = cfg.path("desktop.gui_cache_file") if cfg.get("desktop.gui_cache_file") else \
        cfg.path("workspace.state_dir") / "gui_session.json"
    cal = SessionGuiCalibrator(
        cache,
        layout_overrides=cfg.get("gui.layout_overrides") or {},
    )
    try:
        if args.validate:
            valid = cal.layout_valid()
            print(f"布局验证：{'通过（可复用缓存）' if valid else '失效（需重标定）'}")
            print(json.dumps(cal.stats(), ensure_ascii=False, indent=2, default=str))
            return 0 if valid else 2
        result = cal.calibrate(force=bool(args.force))
        print("标定完成：")
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, default=str))
        print(f"\n缓存：{cache}")
        print("stats:", json.dumps(cal.stats(), ensure_ascii=False, default=str))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"标定失败：{exc}", file=sys.stderr)
        return 2


def cmd_quality(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    from src.router.quality import RouterQualityTracker

    path = cfg.get("router.quality_file") or ".state/router_quality.json"
    qt = RouterQualityTracker(cfg.path("router.quality_file") if cfg.get("router.quality_file") else cfg.path(path))
    summary = qt.summary()
    print("=== Router Recommendation Quality ===")
    if not summary:
        print("  还没有记录")
    for cat, row in sorted(summary.items()):
        print(f"  {cat}:")
        print(f"    tasks={row['tasks']} first_choice_success_rate={row['first_choice_success_rate']} "
              f"fallback_rate={row['fallback_rate']} average_attempts={row['average_attempts']} "
              f"method_regret={row['method_regret']}")
        for m, slot in sorted((row.get("methods") or {}).items()):
            print(f"      {m}: selected={slot.get('selected')} first_ok={slot.get('first_success')} "
                  f"fallback_ok={slot.get('first_fail_then_fallback_ok')} "
                  f"ok={slot.get('ultimate_ok')} fail={slot.get('ultimate_fail')}")
    if args.json:
        _print_json(summary)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
