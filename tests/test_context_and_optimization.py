from datetime import date, timedelta

import pytest
from ortools.sat.python import cp_model

from app.constants import (
    SHIFT_AFTER_NIGHT,
    SHIFT_DAY,
    SHIFT_NIGHT,
    SHIFT_OFF,
    SHIFT_OFF_REQUEST,
    SHIFT_PAID_LEAVE,
    SHIFT_SPECIAL_LEAVE,
    SHIFT_TRAINING,
)
from app.context import build_optimization_context
from app.optimization import (
    SUCCESSFUL_OPTIMIZATION_STATUSES,
    _build_day_ability_balance_objective,
    _log_phase_summary,
    optimize_shift,
)
from app.schemas import GenerateShiftRequest
from app.types import OptimizationError, OptimizationPhaseResult, OptimizerStaff


def make_request(
    *,
    days: int,
    staff_members: list[dict],
    required_day_staff: int | list[int] = 0,
    required_day_staff_override: int | list[int | None] | None = None,
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
    required_day_overrides = (
        required_day_staff_override
        if isinstance(required_day_staff_override, list)
        else [required_day_staff_override] * days
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
                    "required_day_staff_override": required_day_overrides[index],
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


def selected_day_counts(output, *, context) -> list[int]:
    return [
        sum(
            selected_shift_type(
                output,
                staff_id=staff_member.id,
                target_date=target_date,
            )
            == SHIFT_DAY
            for staff_member in context.staff_members
        )
        for target_date in context.month_dates
    ]


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


def test_common_required_day_staff_does_not_change_day_staffing_allocation() -> None:
    staff_members = [staff(staff_id=staff_id) for staff_id in range(1, 7)]
    baseline_context = build_optimization_context(
        make_request(
            days=3,
            staff_members=staff_members,
            off_days={1: 1, 2: 1, 3: 0, 4: 0, 5: 0, 6: 0},
        )
    )
    changed_common_required_context = build_optimization_context(
        make_request(
            days=3,
            staff_members=staff_members,
            required_day_staff=[1, 99, 3],
            off_days={1: 1, 2: 1, 3: 0, 4: 0, 5: 0, 6: 0},
        )
    )

    baseline_output = optimize_shift(baseline_context)
    changed_output = optimize_shift(changed_common_required_context)

    assert selected_day_counts(
        baseline_output, context=baseline_context
    ) == selected_day_counts(changed_output, context=changed_common_required_context)
    assert sorted(selected_day_counts(baseline_output, context=baseline_context)) == [5, 5, 6]


def test_common_high_required_day_is_not_a_day_staffing_exception() -> None:
    staff_members = [staff(staff_id=staff_id) for staff_id in range(1, 7)]
    context = build_optimization_context(
        make_request(
            days=3,
            staff_members=staff_members,
            required_day_staff=[8, 8, 8],
            off_days={1: 1, 2: 1, 3: 0, 4: 0, 5: 0, 6: 0},
        )
    )

    output = optimize_shift(context)

    assert sorted(selected_day_counts(output, context=context)) == [5, 5, 6]


def test_low_day_staffing_override_prefers_floor_target_without_breaking_range() -> None:
    staff_members = [staff(staff_id=staff_id) for staff_id in range(1, 7)]
    context = build_optimization_context(
        make_request(
            days=3,
            staff_members=staff_members,
            required_day_staff_override=[None, None, 3],
            off_days={1: 1, 2: 1, 3: 0, 4: 0, 5: 0, 6: 0},
        )
    )

    output = optimize_shift(context)
    day_counts = selected_day_counts(output, context=context)

    assert day_counts[2] == 5
    assert max(day_counts) - min(day_counts) == 1


def test_high_day_staffing_override_is_reserved_then_rebalanced() -> None:
    staff_members = [staff(staff_id=staff_id) for staff_id in range(1, 8)]
    context = build_optimization_context(
        make_request(
            days=5,
            staff_members=staff_members,
            required_day_staff_override=[None, None, 7, None, None],
            off_days={staff_id: 1 for staff_id in range(1, 8)},
        )
    )

    output = optimize_shift(context)
    day_counts = selected_day_counts(output, context=context)

    assert output.day_staffing_balance_data.total_planned_day_cells == 28
    assert day_counts[2] == 7
    assert sum(day_counts) == 28
    normal_day_counts = [count for index, count in enumerate(day_counts) if index != 2]
    assert max(normal_day_counts) - min(normal_day_counts) == 1


def test_day_staffing_reserves_high_override_then_prefers_low_override_floor_targets() -> None:
    staff_members = [staff(staff_id=staff_id) for staff_id in range(1, 7)]
    context = build_optimization_context(
        make_request(
            days=5,
            staff_members=staff_members,
            required_day_staff_override=[6, 6, 6, 4, 3],
            off_days={1: 1, 2: 1, 3: 0, 4: 0, 5: 0, 6: 0},
        )
    )

    output = optimize_shift(context)
    data = output.day_staffing_balance_data

    assert data.total_planned_day_cells == 28
    assert data.high_day_staffing_overrides == {}
    assert data.normal_floor_target == 5
    assert data.normal_ceil_target == 6
    assert selected_day_counts(output, context=context) == [6, 6, 6, 5, 5]


def test_high_day_staffing_override_is_not_overfilled() -> None:
    staff_members = [staff(staff_id=staff_id) for staff_id in range(1, 9)]
    context = build_optimization_context(
        make_request(
            days=4,
            staff_members=staff_members,
            required_day_staff_override=[8, 5, 5, 4],
            off_days={
                1: 2,
                2: 2,
                3: 1,
                4: 1,
                5: 1,
                6: 1,
                7: 1,
                8: 1,
            },
        )
    )

    output = optimize_shift(context)

    assert output.day_staffing_balance_data.total_planned_day_cells == 22
    assert selected_day_counts(output, context=context) == [8, 5, 5, 4]


def test_low_day_staffing_override_does_not_directly_reduce_day_staffing() -> None:
    staff_members = [staff(staff_id=staff_id) for staff_id in range(1, 7)]
    context = build_optimization_context(
        make_request(
            days=3,
            staff_members=staff_members,
            required_day_staff_override=[6, 6, 3],
            off_days=0,
        )
    )

    output = optimize_shift(context)

    assert selected_day_counts(output, context=context) == [6, 6, 6]


def test_fixed_day_is_included_once_in_total_planned_day_cells() -> None:
    staff_members = [staff(staff_id=1), staff(staff_id=2)]
    context = build_optimization_context(
        make_request(
            days=2,
            staff_members=staff_members,
            fixed_assignments=[
                {"staff_id": 1, "date": "2026-09-01", "shift_type": SHIFT_DAY}
            ],
        )
    )

    output = optimize_shift(context)

    assert output.day_staffing_balance_data.total_planned_day_cells == 4
    assert selected_day_counts(output, context=context) == [2, 2]


def test_night_and_after_night_are_excluded_from_total_planned_day_cells() -> None:
    staff_members = [staff(staff_id=1), staff(staff_id=2)]
    context = build_optimization_context(
        make_request(
            days=3,
            staff_members=staff_members,
            required_night_staff=[1, 0, 0],
            off_days={1: 1, 2: 0},
        )
    )

    output = optimize_shift(context)

    assert output.day_staffing_balance_data.total_planned_day_cells == 3
    assert sum(selected_day_counts(output, context=context)) == 3


def test_off_and_fixed_leave_types_are_excluded_from_total_planned_day_cells() -> None:
    context = build_optimization_context(
        make_request(
            days=5,
            staff_members=[staff(staff_id=1)],
            off_days=0,
            fixed_assignments=[
                {"staff_id": 1, "date": "2026-09-01", "shift_type": SHIFT_OFF},
                {
                    "staff_id": 1,
                    "date": "2026-09-02",
                    "shift_type": SHIFT_OFF_REQUEST,
                },
                {
                    "staff_id": 1,
                    "date": "2026-09-03",
                    "shift_type": SHIFT_PAID_LEAVE,
                },
                {
                    "staff_id": 1,
                    "date": "2026-09-04",
                    "shift_type": SHIFT_SPECIAL_LEAVE,
                },
                {
                    "staff_id": 1,
                    "date": "2026-09-05",
                    "shift_type": SHIFT_TRAINING,
                },
            ],
        )
    )

    output = optimize_shift(context)

    assert output.day_staffing_balance_data.total_planned_day_cells == 0
    assert selected_day_counts(output, context=context) == [0, 0, 0, 0, 0]


def test_day_ability_phase_preserves_fixed_day_staffing_counts() -> None:
    staff_members = [
        {**staff(staff_id=1), "ability_level": 1},
        {**staff(staff_id=2), "ability_level": 2},
        {**staff(staff_id=3), "ability_level": 3},
        {**staff(staff_id=4), "ability_level": 4},
        {**staff(staff_id=5), "ability_level": 5},
        {**staff(staff_id=6), "ability_level": 5},
    ]
    context = build_optimization_context(
        make_request(
            days=3,
            staff_members=staff_members,
            required_day_staff_override=[None, None, 3],
            off_days={1: 1, 2: 1, 3: 0, 4: 0, 5: 0, 6: 0},
        )
    )

    output = optimize_shift(context)

    expected_day_counts = selected_day_counts(output, context=context)
    assert expected_day_counts[2] == 5
    assert sorted(expected_day_counts) == [5, 5, 6]
    assert [
        output.solver.Value(count_var)
        for count_var in output.day_staffing_balance_data.actual_day_count_vars.values()
    ] == expected_day_counts


def test_phase_summary_logs_feasible_unknown_and_not_run_as_non_optimal(caplog) -> None:
    phase_results = [
        OptimizationPhaseResult("night_count_balance", "OPTIMAL", 0, True, None),
        OptimizationPhaseResult("night_ability_balance", "FEASIBLE", 4, False, None),
        OptimizationPhaseResult("day_staffing_balance", "UNKNOWN", None, False, None),
        OptimizationPhaseResult("day_ability_balance", "NOT_RUN", None, False, None),
    ]

    with caplog.at_level("INFO", logger="app.optimization"):
        _log_phase_summary(phase_results)

    assert "name=night_count_balance status=OPTIMAL optimal=True" in caplog.text
    assert "name=night_ability_balance status=FEASIBLE optimal=False" in caplog.text
    assert "optimization non_optimal_phases=['night_ability_balance', 'day_staffing_balance', 'day_ability_balance']" in caplog.text
