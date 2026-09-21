from datetime import date, timedelta

import pytest
from ortools.sat.python import cp_model

from app.constants import (
    SHIFT_AFTER_NIGHT,
    SHIFT_DAY,
    SHIFT_NIGHT,
    SHIFT_OFF,
    SHIFT_PAID_LEAVE,
    SHIFT_SPECIAL_LEAVE,
)
from app.context import build_optimization_context
from app.optimization import (
    SUCCESSFUL_OPTIMIZATION_STATUSES,
    _build_day_ability_balance_objective,
    optimize_shift,
)
from app.schemas import GenerateShiftRequest
from app.types import OptimizationError, OptimizerStaff


def make_request(
    *,
    days: int,
    staff_members: list[dict],
    required_day_staff: int | list[int] = 0,
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
    required_days = (
        required_day_staff
        if isinstance(required_day_staff, list)
        else [required_day_staff] * days
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
                    "required_day_staff": required_days[index],
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


def test_day_ability_balance_spreads_level_composition_across_days() -> None:
    staff_members = [
        {
            **staff(staff_id=index),
            "ability_level": ability_level,
        }
        for index, ability_level in enumerate((1, 1, 3, 3, 5, 5), start=1)
    ]
    context = build_optimization_context(
        make_request(
            days=2,
            staff_members=staff_members,
            required_day_staff=3,
            off_days=1,
        )
    )

    output = optimize_shift(context)

    for target_date in context.month_dates:
        selected_levels = [
            staff_member.ability_level
            for staff_member in context.staff_members
            if selected_shift_type(
                output,
                staff_id=staff_member.id,
                target_date=target_date,
            )
            == SHIFT_DAY
        ]
        assert sorted(selected_levels) == [1, 3, 5]


def test_day_ability_balance_counts_levels_with_different_daily_headcounts() -> None:
    model = cp_model.CpModel()
    first_date = date(2026, 9, 1)
    second_date = date(2026, 9, 2)
    staff_members = [
        OptimizerStaff(1, "member", 1, True, ()),
        OptimizerStaff(2, "member", 3, True, ()),
        OptimizerStaff(3, "member", 5, True, ()),
    ]
    shift_vars = {
        (staff_member.id, target_date): {
            SHIFT_DAY: model.NewBoolVar(
                f"day_{staff_member.id}_{target_date.isoformat()}"
            )
        }
        for staff_member in staff_members
        for target_date in (first_date, second_date)
    }
    actual_day_count_vars = {}
    for target_date in (first_date, second_date):
        count_var = model.NewIntVar(0, 3, f"day_count_{target_date.isoformat()}")
        model.Add(
            count_var
            == sum(
                shift_vars[(staff_member.id, target_date)][SHIFT_DAY]
                for staff_member in staff_members
            )
        )
        actual_day_count_vars[target_date] = count_var
    for staff_member in staff_members:
        model.Add(
            shift_vars[(staff_member.id, first_date)][SHIFT_DAY]
            == int(staff_member.id in {1, 2})
        )
        model.Add(
            shift_vars[(staff_member.id, second_date)][SHIFT_DAY]
            == int(staff_member.id == 3)
        )

    data = _build_day_ability_balance_objective(
        model=model,
        month_dates=[first_date, second_date],
        shift_vars=shift_vars,
        staff_members=staff_members,
        actual_day_count_vars=actual_day_count_vars,
    )
    solver = cp_model.CpSolver()

    assert solver.Solve(model) in {
        cp_model.OPTIMAL,
        cp_model.FEASIBLE,
    }
    assert data.staff_level_counts == {1: 1, 2: 0, 3: 1, 4: 0, 5: 1}
    assert solver.Value(data.level_count_vars[(first_date, 1)]) == 1
    assert solver.Value(data.level_count_vars[(first_date, 3)]) == 1
    assert solver.Value(data.level_count_vars[(second_date, 5)]) == 1
    assert solver.Value(data.daily_ability_total_vars[first_date]) == 4
    assert solver.Value(data.daily_ability_total_vars[second_date]) == 5


def test_night_ability_balance_uses_only_night_eligible_average() -> None:
    staff_members = [
        {
            **staff(staff_id=index),
            "ability_level": ability_level,
        }
        for index, ability_level in enumerate((2, 3, 4, 5), start=1)
    ]
    staff_members.append(
        {
            **staff(staff_id=5, can_night_shift=False),
            "ability_level": 5,
        }
    )
    context = build_optimization_context(
        make_request(
            days=2,
            staff_members=staff_members,
            required_night_staff=2,
        )
    )

    output = optimize_shift(context)

    nightly_ability_totals = []
    night_counts = {staff_member.id: 0 for staff_member in context.staff_members}
    for target_date in context.month_dates:
        night_staff = [
            staff_member
            for staff_member in context.staff_members
            if selected_shift_type(
                output,
                staff_id=staff_member.id,
                target_date=target_date,
            )
            == SHIFT_NIGHT
        ]
        nightly_ability_totals.append(
            sum(staff_member.ability_level for staff_member in night_staff)
        )
        for staff_member in night_staff:
            night_counts[staff_member.id] += 1

    assert nightly_ability_totals == [7, 7]
    assert night_counts == {1: 1, 2: 1, 3: 1, 4: 1, 5: 0}


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


def test_manual_only_night_after_day_is_preserved() -> None:
    override_keys = [
        {"staff_id": 1, "date": f"2026-09-0{day}"}
        for day in (1, 2, 3)
    ]
    context = build_optimization_context(
        make_request(
            days=3,
            staff_members=[staff(staff_id=1)],
            required_night_staff=[1, 0, 0],
            fixed_assignments=[
                {"staff_id": 1, "date": "2026-09-01", "shift_type": SHIFT_NIGHT},
                {"staff_id": 1, "date": "2026-09-02", "shift_type": SHIFT_AFTER_NIGHT},
                {"staff_id": 1, "date": "2026-09-03", "shift_type": SHIFT_DAY},
            ],
            user_override_assignment_keys=override_keys,
        )
    )

    output = optimize_shift(context)

    assert [
        selected_shift_type(output, staff_id=1, target_date=target_date)
        for target_date in context.month_dates
    ] == [SHIFT_NIGHT, SHIFT_AFTER_NIGHT, SHIFT_DAY]


def test_generated_night_cannot_use_manual_day_after_after_night() -> None:
    context = build_optimization_context(
        make_request(
            days=3,
            staff_members=[staff(staff_id=1), staff(staff_id=2)],
            required_night_staff=[1, 0, 0],
            off_days=1,
            fixed_assignments=[
                {"staff_id": 1, "date": "2026-09-03", "shift_type": SHIFT_DAY},
            ],
            user_override_assignment_keys=[
                {"staff_id": 1, "date": "2026-09-03"},
            ],
        )
    )

    output = optimize_shift(context)

    assert selected_shift_type(
        output, staff_id=1, target_date=context.month_dates[0]
    ) != SHIFT_NIGHT
    assert [
        selected_shift_type(output, staff_id=2, target_date=target_date)
        for target_date in context.month_dates
    ] == [SHIFT_NIGHT, SHIFT_AFTER_NIGHT, SHIFT_OFF]


def test_low_required_day_does_not_reduce_actual_day_staffing() -> None:
    staff_members = [staff(staff_id=staff_id) for staff_id in range(1, 7)]
    request = make_request(
        days=3,
        staff_members=staff_members,
        off_days=0,
    )
    payload = request.model_dump(mode="json")
    for rule, required_day_staff in zip(
        payload["effective_rules"],
        [5, 5, 3],
    ):
        rule["required_day_staff"] = required_day_staff
    context = build_optimization_context(
        GenerateShiftRequest.model_validate(payload)
    )

    output = optimize_shift(context)

    assert [
        sum(
            selected_shift_type(
                output,
                staff_id=staff_member["id"],
                target_date=target_date,
            )
            == SHIFT_DAY
            for staff_member in staff_members
        )
        for target_date in context.month_dates
    ] == [6, 6, 6]


def test_high_required_day_is_filled_when_capacity_allows() -> None:
    staff_members = [staff(staff_id=staff_id) for staff_id in range(1, 8)]
    request = make_request(
        days=3,
        staff_members=staff_members,
        off_days=0,
    )
    payload = request.model_dump(mode="json")
    payload["effective_rules"][1]["required_day_staff"] = 7
    context = build_optimization_context(
        GenerateShiftRequest.model_validate(payload)
    )

    output = optimize_shift(context)

    assert sum(
        selected_shift_type(
            output,
            staff_id=staff_member["id"],
            target_date=context.month_dates[1],
        )
        == SHIFT_DAY
        for staff_member in staff_members
    ) == 7
