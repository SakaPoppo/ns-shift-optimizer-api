"""Conversion from HTTP schemas to the optimizer's internal data model."""

from .schemas import GenerateShiftRequest
from .types import EffectiveRule, OptimizationContext, OptimizerStaff


def build_optimization_context(request: GenerateShiftRequest) -> OptimizationContext:
    """Build the mapping-based structure consumed by the OR-Tools optimizer."""

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
        user_override_assignment_keys={
            (item.staff_id, item.date)
            for item in request.user_override_assignment_keys
        },
    )
