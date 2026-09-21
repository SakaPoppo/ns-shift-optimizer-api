"""Conversion from HTTP schemas to the optimizer's internal data model."""

from .constants import GENERATABLE_SHIFT_TYPES, OFF_LIKE_SHIFT_TYPES, SHIFT_TRAINING
from .schemas import GenerateShiftRequest
from .types import (
    EffectiveRule,
    OptimizationContext,
    OptimizationError,
    OptimizerStaff,
)


def build_optimization_context(request: GenerateShiftRequest) -> OptimizationContext:
    """Build the mapping-based structure consumed by the OR-Tools optimizer."""

    _validate_request_consistency(request)

    return OptimizationContext(
        month_dates=list(request.month_dates),
        staff_members=[
            OptimizerStaff(
                id=staff.id,
                role=staff.role,
                ability_level=staff.ability_level,
                can_night_shift=staff.can_night_shift,
                regular_days_off=tuple(staff.regular_days_off),
            )
            for staff in request.staff_members
        ],
        fixed_assignments={
            (assignment.staff_id, assignment.date): assignment.shift_type
            for assignment in request.fixed_assignments
        },
        effective_rules={
            rule.date: EffectiveRule(
                required_day_staff=rule.required_day_staff,
                required_day_staff_override=rule.required_day_staff_override,
                required_night_staff=rule.required_night_staff,
                required_leader_staff=rule.required_leader_staff,
                min_ability_level=rule.min_ability_level,
                min_ability_level_staff_count=(
                    rule.min_ability_level_staff_count
                ),
                max_consecutive_work_days=rule.max_consecutive_work_days,
                night_shift_next_day_off=rule.night_shift_next_day_off,
            )
            for rule in request.effective_rules
        },
        previous_consecutive_work_days={
            item.staff_id: item.previous_consecutive_work_days
            for item in request.previous_consecutive_work_days
        },
        effective_off_days={
            item.staff_id: item.off_days for item in request.effective_off_days
        },
        configured_off_days=(
            {
                item.staff_id: item.off_days
                for item in request.configured_off_days
            }
            or {
                item.staff_id: item.off_days
                for item in request.effective_off_days
            }
        ),
        user_override_assignment_keys={
            (item.staff_id, item.date)
            for item in request.user_override_assignment_keys
        },
    )


def _validate_request_consistency(request: GenerateShiftRequest) -> None:
    """Reject cross-field inconsistencies before constructing an OR-Tools model."""

    month_date_set = set(request.month_dates)
    if not request.month_dates:
        raise OptimizationError("対象日がないため最適化できません。")
    if len(month_date_set) != len(request.month_dates):
        raise OptimizationError("month_dates に重複した日付があります。")

    staff_ids = [staff.id for staff in request.staff_members]
    staff_id_set = set(staff_ids)
    if not staff_ids:
        raise OptimizationError("staff_members がないため最適化できません。")
    if len(staff_id_set) != len(staff_ids):
        raise OptimizationError("staff_members に重複した staff_id があります。")

    rule_dates = [rule.date for rule in request.effective_rules]
    if len(set(rule_dates)) != len(rule_dates) or set(rule_dates) != month_date_set:
        raise OptimizationError(
            "effective_rules は month_dates の全日付に対して1件ずつ必要です。"
        )
    if len({rule.max_consecutive_work_days for rule in request.effective_rules}) != 1:
        raise OptimizationError(
            "max_consecutive_work_days は全 effective_rules で一致している必要があります。"
        )

    off_day_staff_ids = [item.staff_id for item in request.effective_off_days]
    if (
        len(set(off_day_staff_ids)) != len(off_day_staff_ids)
        or set(off_day_staff_ids) != staff_id_set
    ):
        raise OptimizationError(
            "effective_off_days は全 staff_members に対して1件ずつ必要です。"
        )

    configured_off_day_staff_ids = [
        item.staff_id for item in request.configured_off_days
    ]
    if configured_off_day_staff_ids and (
        len(set(configured_off_day_staff_ids))
        != len(configured_off_day_staff_ids)
        or set(configured_off_day_staff_ids) != staff_id_set
    ):
        raise OptimizationError(
            "configured_off_days は全 staff_members に対して1件ずつ必要です。"
        )

    _validate_assignment_cells(
        label="fixed_assignments",
        cells=[(item.staff_id, item.date) for item in request.fixed_assignments],
        staff_id_set=staff_id_set,
        month_date_set=month_date_set,
    )
    valid_shift_types = set(GENERATABLE_SHIFT_TYPES) | OFF_LIKE_SHIFT_TYPES | {
        SHIFT_TRAINING
    }
    if any(
        item.shift_type not in valid_shift_types
        for item in request.fixed_assignments
    ):
        raise OptimizationError("fixed_assignments に未対応の shift_type があります。")

    _validate_assignment_cells(
        label="user_override_assignment_keys",
        cells=[
            (item.staff_id, item.date)
            for item in request.user_override_assignment_keys
        ],
        staff_id_set=staff_id_set,
        month_date_set=month_date_set,
    )

    previous_staff_ids = [
        item.staff_id for item in request.previous_consecutive_work_days
    ]
    if (
        len(set(previous_staff_ids)) != len(previous_staff_ids)
        or not set(previous_staff_ids).issubset(staff_id_set)
    ):
        raise OptimizationError(
            "previous_consecutive_work_days に不正な staff_id があります。"
        )


def _validate_assignment_cells(*, label, cells, staff_id_set, month_date_set) -> None:
    """Ensure cell-keyed payload fields refer to a unique in-month staff cell."""

    if len(set(cells)) != len(cells):
        raise OptimizationError(f"{label} に重複したスタッフ・日付の組み合わせがあります。")
    if any(staff_id not in staff_id_set for staff_id, _ in cells):
        raise OptimizationError(f"{label} に staff_members に存在しない staff_id があります。")
    if any(target_date not in month_date_set for _, target_date in cells):
        raise OptimizationError(f"{label} に month_dates に存在しない日付があります。")
