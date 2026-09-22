"""验证子系统。

统一原则：**写入操作的返回值不算证据，重新读回来的状态才算。**
"""

from .result import CheckResult, VerifyReport
from .verifiers import (
    DEFAULT_POS_TOL,
    DEFAULT_ROT_TOL,
    DEFAULT_SCALE_TOL,
    Verifier,
    as_list,
    check_actor_found,
    check_axis_delta,
    check_dirty_empty,
    check_file_written,
    check_floating_resolved,
    check_save_result,
    check_transform_equals,
    check_unchanged,
    check_visual_changed,
)

__all__ = [
    "CheckResult",
    "VerifyReport",
    "Verifier",
    "as_list",
    "check_actor_found",
    "check_transform_equals",
    "check_axis_delta",
    "check_unchanged",
    "check_save_result",
    "check_dirty_empty",
    "check_file_written",
    "check_visual_changed",
    "check_floating_resolved",
    "DEFAULT_POS_TOL",
    "DEFAULT_ROT_TOL",
    "DEFAULT_SCALE_TOL",
]
