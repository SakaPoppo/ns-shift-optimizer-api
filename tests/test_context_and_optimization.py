from datetime import date, timedelta

import pytest

from app.constants import (
    SHIFT_AFTER_NIGHT,
    SHIFT_DAY,
    SHIFT_NIGHT,
    SHIFT_OFF,
    SHIFT_PAID_LEAVE,
    SHIFT_SPECIAL_LEAVE,
)
from app.context import build_optimization_context
from app.optimization import SUCCESSFUL_OPTIMIZATION_STATUSES, optimize_shift
from app.schemas import GenerateShiftRequest
from app.types import OptimizationError


def make_request(
    *,
    days: int,
    staff_members: list[dict],
    required_night_staff: int | list[int] = 0,
    off_days: int | dict[int, int] = 0,
    max_consecutive_work_days: int = 5,
    fixed_assignments: list[dict] | None = None,
    previous_consecutive_work_days: list[dict] | None = None,
    user_override_assignment_keys: list[dict] | None = None,
) -> GenerateShiftRequest:
    start_date = date(2026, 9, 1)
    month_dates = [start_date + timedelta(days=index) for index in range(days)]
    required_nights = (
        required_night_staff
        if isinstance(required_night_staff, list)
        else [required_night_staff] * days
    )
    off_days_by_staff = (
        off_days
        if isinstance(off_days, dict)
        else {staff["id"]: off_days for staff in staff_members}
    )
    return GenerateShiftRequest.model_validate(
        {
            "month_dates": [item.isoformat() for item in month_dates],
            "staff_members": staff_members,
            "fixed_assignments": fixed_assignments or [],
            "effective_rules": [
                {
                    "date": target_date.isoformat(),
                    "required_day_staff": 0,
                    "required_night_staff": required_nights[index],
                    "required_leader_staff": 0,
                    "min_ability_level": None,
                    "min_ability_level_staff_count": None,
                    "max_consecutive_work_days": max_consecutive_work_days,
                    "night_shift_next_day_off": True,
                }
                for index, target_date in enumerate(month_dates)
            ],
            "previous_consecutive_work_days": previous_consecutive_work_days
            or [],
            "effective_off_days": [
                {"staff_id": staff_id, "off_days": count}
                for staff_id, count in off_days_by_staff.items()
            ],
            "configured_off_days": [
                {"staff_id": staff_id, "off_days": count}
                for staff_id, count in off_days_by_staff.items()
            ],
            "user_override_assignment_keys": user_override_assignment_keys or [],
        }
    )


def staff(*, staff_id: int, can_night_shift: bool = True) -> dict:
    return {
        "id": staff_id,
        "role": "leader" if staff_id == 1 else "member",
        "ability_level": 4,
        "can_night_shift": can_night_shift,
        "regular_days_off": [],
    }


def selected_shift_type(output, *, staff_id: int, target_date: date) -> str | None:
    day_vars = output.shift_vars[(staff_id, target_date)]
    for shift_type, shift_var in day_vars.items():
        if output.solver.Value(shift_var):
            return shift_type
    return None


def test_build_optimization_context_converts_payload_collections() -> None:
    request = make_request(
        days=2,
        staff_members=[staff(staff_id=1)],
        off_days=1,
        fixed_assignments=[
            {"staff_id": 1, "date": "2026-09-01", "shift_type": SHIFT_NIGHT}
        ],
        previous_consecutive_work_days=[
            {"staff_id": 1, "previous_consecutive_work_days": 4}
        ],
        user_override_assignment_keys=[{"staff_id": 1, "date": "2026-09-01"}],
    )

    context = build_optimization_context(request)

    first_date, second_date = context.month_dates
    assert context.staff_members[0].regular_days_off == ()
    assert context.fixed_assignments == {(1, first_date): SHIFT_NIGHT}
    assert context.effective_rules[first_date].required_night_staff == 0
    assert context.effective_rules[second_date].max_consecutive_work_days == 5
    assert context.previous_consecutive_work_days == {1: 4}
    assert context.effective_off_days == {1: 1}
    assert context.user_override_assignment_keys == {(1, first_date)}


def test_optimize_shift_solves_a_small_context() -> None:
    context = build_optimization_context(
        make_request(
            days=3,
            staff_members=[staff(staff_id=1), staff(staff_id=2)],
            off_days=1,
        )
    )

    output = optimize_shift(context)

    assert output.solver_status in SUCCESSFUL_OPTIMIZATION_STATUSES
    assert len(output.phase_results) == 5


def test_optimize_shift_preserves_fixed_night_assignment() -> None:
    context = build_optimization_context(
        make_request(
            days=3,
            staff_members=[staff(staff_id=1), staff(staff_id=2)],
            required_night_staff=[1, 0, 0],
            off_days={1: 1, 2: 0},
            fixed_assignments=[
                {"staff_id": 1, "date": "2026-09-01", "shift_type": SHIFT_NIGHT}
            ],
        )
    )

    output = optimize_shift(context)
    first_date, second_date, third_date = context.month_dates

    assert selected_shift_type(output, staff_id=1, target_date=first_date) == SHIFT_NIGHT
    assert selected_shift_type(output, staff_id=1, target_date=second_date) == SHIFT_AFTER_NIGHT
    assert selected_shift_type(output, staff_id=1, target_date=third_date) == SHIFT_OFF


def test_optimize_shift_excludes_paid_and_special_leave_from_monthly_off_days() -> None:
    context = build_optimization_context(
        make_request(
            days=4,
            staff_members=[staff(staff_id=1)],
            off_days=1,
            max_consecutive_work_days=5,
            fixed_assignments=[
                {"staff_id": 1, "date": "2026-09-01", "shift_type": SHIFT_PAID_LEAVE},
                {"staff_id": 1, "date": "2026-09-02", "shift_type": SHIFT_SPECIAL_LEAVE},
            ],
        )
    )

    output = optimize_shift(context)

    assert output.solver_status in SUCCESSFUL_OPTIMIZATION_STATUSES
    assert sum(
        selected_shift_type(output, staff_id=1, target_date=target_date) == SHIFT_OFF
        for target_date in context.month_dates[2:]
    ) == 1


def test_night_ineligible_staff_is_never_assigned_night() -> None:
    context = build_optimization_context(
        make_request(
            days=1,
            staff_members=[
                staff(staff_id=1, can_night_shift=False),
                staff(staff_id=2, can_night_shift=True),
            ],
            required_night_staff=1,
        )
    )

    output = optimize_shift(context)

    assert selected_shift_type(
        output, staff_id=1, target_date=context.month_dates[0]
    ) != SHIFT_NIGHT


def test_previous_consecutive_work_days_limit_month_start_work() -> None:
    context = build_optimization_context(
        make_request(
            days=1,
            staff_members=[staff(staff_id=1)],
            off_days=1,
            max_consecutive_work_days=5,
            previous_consecutive_work_days=[
                {"staff_id": 1, "previous_consecutive_work_days": 5}
            ],
        )
    )

    output = optimize_shift(context)

    assert selected_shift_type(
        output,
        staff_id=1,
        target_date=context.month_dates[0],
    ) == SHIFT_OFF


def test_context_rejects_inconsistent_max_consecutive_work_days() -> None:
    request = make_request(days=2, staff_members=[staff(staff_id=1)])
    inconsistent_payload = request.model_dump(mode="json")
    inconsistent_payload["effective_rules"][1]["max_consecutive_work_days"] = 4

    with pytest.raises(OptimizationError, match="max_consecutive_work_days"):
        build_optimization_context(
            GenerateShiftRequest.model_validate(inconsistent_payload)
        )


def test_user_override_work_streak_is_preserved_then_forces_generated_off() -> None:
    override_keys = [
        {"staff_id": 1, "date": f"2026-09-0{day}"} for day in (1, 2, 3)
    ]
    fixed_assignments = [
        {"staff_id": 1, "date": item["date"], "shift_type": SHIFT_DAY}
        for item in override_keys
    ]
    context = build_optimization_context(
        make_request(
            days=4,
            staff_members=[staff(staff_id=1)],
            off_days=1,
            max_consecutive_work_days=2,
            fixed_assignments=fixed_assignments,
            user_override_assignment_keys=override_keys,
        )
    )

    output = optimize_shift(context)

    assert selected_shift_type(
        output, staff_id=1, target_date=context.month_dates[3]
    ) == SHIFT_OFF
