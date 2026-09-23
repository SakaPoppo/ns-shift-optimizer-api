"""OR-Toolsの制約モデル構築と複数フェーズ最適化。

処理の流れは、勤務セルの変数作成、ハード制約追加、目的関数の組み立て、
優先順位付きフェーズの順次探索、各フェーズの最良値固定である。
前フェーズの目的値を固定してから次フェーズを解くため、後続の改善で
夜勤回数公平性など上位優先度の品質が悪化しない。

主なハード制約は必要リーダー人数・能力条件・最大連勤・月休日数・必要夜勤人数。
主なソフト目的は夜勤回数公平性、日勤人数配分、日勤・夜勤の能力均等化、長期連勤の抑制。
"""

from __future__ import annotations

import logging
import math
import os

from ortools.sat.python import cp_model

from .constants import (
    GENERATABLE_SHIFT_TYPES,
    FIXED_NON_GENERATED_MONTHLY_OFF_SHIFT_TYPES,
    MONTHLY_OFF_SHIFT_TYPES,
    OFF_LIKE_SHIFT_TYPES,
    ROLE_LEADER,
    SHIFT_AFTER_NIGHT,
    SHIFT_DAY,
    SHIFT_NIGHT,
    SHIFT_OFF,
    SHIFT_OFF_REQUEST,
    SHIFT_TRAINING,
    WORKLIKE_SHIFT_TYPES,
)
from .types import (
    DayAbilityBalanceData,
    DayStaffingBalanceData,
    InfeasibleOptimizationError,
    NightCountBalanceData,
    NightAbilityBalanceData,
    OptimizationContext,
    OptimizationError,
    OptimizationPhaseDefinition,
    OptimizationPhaseResult,
    ShiftOptimizationOutput,
)


logger = logging.getLogger(__name__)


LONG_STREAK_WEIGHTS = {"near_max": 1, "at_max": 3}
ABILITY_LEVELS = (1, 2, 3, 4, 5)
CP_SAT_INT_MAX = 2**63 - 1
PHASE_TIME_LIMITS = {
    "night_count_balance": 40,
    "night_ability_balance": 40,
    "day_staffing_balance": 40,
    "day_ability_balance": 40,
    "long_streak": 40,
}
REQUIRED_OPTIMIZATION_PHASES = {
    "night_count_balance",
    "day_staffing_balance",
}
SUCCESSFUL_OPTIMIZATION_STATUSES = {"OPTIMAL", "FEASIBLE"}


def optimize_shift(context: OptimizationContext) -> ShiftOptimizationOutput:
    """読み込み済みコンテキストから制約モデルを作り、優先順に最適化する。"""

    # 1人・1日・1勤務の選択肢と、固定勤務の土台を作る。
    model = cp_model.CpModel()
    shift_vars = _build_shift_variables(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        fixed_assignments=context.fixed_assignments,
    )
    # 実行可能性を守るハード制約を追加する。
    _add_night_shift_eligibility_constraints(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
    )
    _add_next_month_first_regular_day_off_constraints(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
    )
    night_after_night_pattern_terms = _add_night_pattern_constraints(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        fixed_assignments=context.fixed_assignments,
        effective_rules=context.effective_rules,
        user_override_assignment_keys=context.user_override_assignment_keys,
    )
    _add_monthly_off_day_constraints(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        fixed_assignments=context.fixed_assignments,
        effective_off_days=context.effective_off_days,
    )

    # 日勤・夜勤・能力・長期連勤の最適化用データと目的関数を作る。
    _add_required_night_staff_constraints(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        effective_rules=context.effective_rules,
    )
    day_staffing_balance_data = _build_day_staffing_balance_data(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        effective_rules=context.effective_rules,
        fixed_assignments=context.fixed_assignments,
        effective_off_days=context.effective_off_days,
    )
    day_ability_balance_data = _build_day_ability_balance_objective(
        model=model,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        staff_members=context.staff_members,
        actual_day_count_vars=day_staffing_balance_data.actual_day_count_vars,
    )
    _add_staffing_safety_constraints(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        effective_rules=context.effective_rules,
    )
    _add_max_consecutive_work_constraints(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        fixed_assignments=context.fixed_assignments,
        max_consecutive_work_days=context.max_consecutive_work_days,
        previous_consecutive_work_days=context.previous_consecutive_work_days,
        user_override_assignment_keys=context.user_override_assignment_keys,
    )
    night_eligible_staff = [
        staff
        for staff in context.staff_members
        if staff.can_night_shift
    ]
    night_ability_balance_data = _build_night_ability_balance_objective(
        model=model,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        effective_rules=context.effective_rules,
        night_eligible_staff=night_eligible_staff,
    )
    night_count_balance_data = _build_night_count_balance_objective(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        night_after_night_pattern_terms=night_after_night_pattern_terms,
        night_ability_balance_data=night_ability_balance_data,
    )
    long_streak_terms = _add_long_consecutive_work_objective(
        model=model,
        staff_members=context.staff_members,
        month_dates=context.month_dates,
        shift_vars=shift_vars,
        fixed_assignments=context.fixed_assignments,
        max_consecutive_work_days=context.max_consecutive_work_days,
        previous_consecutive_work_days=context.previous_consecutive_work_days,
    )

    # 上位目的の最良値を固定しながら、定義順に各フェーズを探索する。
    phase_definitions = _build_phase_definitions(
        day_staffing_balance_data=day_staffing_balance_data,
        day_ability_balance_data=day_ability_balance_data,
        night_count_balance_data=night_count_balance_data,
        long_streak_terms=long_streak_terms,
    )
    logger.info(
        "shift optimization configuration "
        "staff_count=%s phase_limits=%s",
        len(context.staff_members),
        {
            phase.name: phase.max_time_seconds
            for phase in phase_definitions
        },
    )
    phase_results, solver, solver_status = _run_optimization_phases(
        model=model,
        shift_vars=shift_vars,
        actual_day_count_vars=(
            day_staffing_balance_data.actual_day_count_vars
        ),
        phase_definitions=phase_definitions,
    )
    _log_phase_summary(phase_results)

    return ShiftOptimizationOutput(
        solver=solver,
        solver_status=solver_status,
        shift_vars=shift_vars,
        day_staffing_balance_data=day_staffing_balance_data,
        night_count_balance_data=night_count_balance_data,
        long_streak_terms=long_streak_terms,
        phase_results=phase_results,
    )


def _build_phase_definitions(
    *,
    day_staffing_balance_data: DayStaffingBalanceData,
    day_ability_balance_data: DayAbilityBalanceData,
    night_count_balance_data: NightCountBalanceData,
    long_streak_terms: list,
) -> list[OptimizationPhaseDefinition]:
    """現在の優先順位とフェーズ共通の制限時間でフェーズを作る。"""
    objectives = [
    (
        "night_count_balance",
        night_count_balance_data.night_balance_violation,
    ),
    (
        "night_ability_balance",
        night_count_balance_data.objective_score,
    ),
    (
        "day_staffing_balance",
        day_staffing_balance_data.objective_score,
    ),
    (
        "day_ability_balance",
        day_ability_balance_data.objective_score,
    ),
    (
        "long_streak",
        sum(long_streak_terms) if long_streak_terms else 0,
    ),
]
    return [
        OptimizationPhaseDefinition(
            name=name,
            objective=objective,
            max_time_seconds=PHASE_TIME_LIMITS[name],
        )
        for name, objective in objectives
    ]


def _add_shift_solution_hints(*, model, shift_vars: dict, solver) -> None:
    """直前の有効解に含まれるシフト配置だけを次の探索へ渡す。"""

    model.ClearHints()
    for day_vars in shift_vars.values():
        for shift_var in day_vars.values():
            model.AddHint(shift_var, solver.Value(shift_var))


def _fix_night_assignments(*, model, shift_vars: dict, solver) -> None:
    """夜勤フェーズで採用したNIGHT配置を後続フェーズ向けに固定する。"""

    for day_vars in shift_vars.values():
        night_var = day_vars[SHIFT_NIGHT]
        model.Add(night_var == solver.Value(night_var))


def _fix_day_staffing_counts(
    *, model, actual_day_count_vars: dict, solver
) -> None:
    """日勤人数フェーズで採用した各日の人数を後続フェーズ向けに固定する。"""

    for count_var in actual_day_count_vars.values():
        model.Add(count_var == solver.Value(count_var))


def _run_optimization_phases(
    *,
    model,
    shift_vars: dict,
    actual_day_count_vars: dict,
    phase_definitions: list[OptimizationPhaseDefinition],
) -> tuple[list[OptimizationPhaseResult], object, str]:
    """フェーズを順に解き、直前の有効な配置を次フェーズへ引き継ぐ。"""

    if not phase_definitions:
        raise OptimizationError("最適化フェーズが定義されていません。")

    phase_results = []
    last_successful_solver = None
    last_successful_status = None
    last_successful_phase_name = None
    completed_successful_phase_names = set()

    for phase_index, phase in enumerate(phase_definitions):
        if last_successful_solver is not None:
            _add_shift_solution_hints(
                model=model,
                shift_vars=shift_vars,
                solver=last_successful_solver,
            )
        result = _solve_and_fix_objective(
            model=model,
            objective=phase.objective,
            phase_name=phase.name,
            max_time_seconds=phase.max_time_seconds,
        )
        phase_results.append(result)

        if result.status == "UNKNOWN":
            if (
                phase.name == "night_ability_balance"
                and last_successful_solver is not None
                and last_successful_phase_name == "night_count_balance"
            ):
                _fix_night_assignments(
                    model=model,
                    shift_vars=shift_vars,
                    solver=last_successful_solver,
                )
                logger.warning(
                    "night ability optimization fallback "
                    "using night_count_balance solution"
                )
                continue

            if (
                last_successful_solver is None
                or not REQUIRED_OPTIMIZATION_PHASES.issubset(
                    completed_successful_phase_names
                )
            ):
                raise OptimizationError(
                    _build_solver_error_message(result.status)
                )

            phase_results.extend(
                OptimizationPhaseResult(
                    name=remaining_phase.name,
                    status="NOT_RUN",
                    objective_value=None,
                    optimal=False,
                    solver=None,
                )
                for remaining_phase in phase_definitions[phase_index + 1 :]
            )

            logger.warning(
                "shift optimization fallback stopped_phase=%s "
                "last_successful_phase=%s status=%s",
                phase.name,
                last_successful_phase_name,
                last_successful_status,
            )
            break

        if result.status == "INFEASIBLE":
            raise InfeasibleOptimizationError()
        if result.status not in SUCCESSFUL_OPTIMIZATION_STATUSES:
            raise OptimizationError(
                _build_solver_error_message(result.status)
            )
        if phase.name == "night_ability_balance":
            _fix_night_assignments(
                model=model,
                shift_vars=shift_vars,
                solver=result.solver,
            )
        elif phase.name == "day_staffing_balance":
            _fix_day_staffing_counts(
                model=model,
                actual_day_count_vars=actual_day_count_vars,
                solver=result.solver,
            )
        last_successful_solver = result.solver
        last_successful_status = result.status
        last_successful_phase_name = result.name
        completed_successful_phase_names.add(result.name)

    if last_successful_solver is None or last_successful_status is None:
        raise OptimizationError("有効な最適化結果を取得できませんでした。")

    logger.info(
        "shift optimization completed last_successful_phase=%s status=%s",
        last_successful_phase_name,
        last_successful_status,
    )
    return phase_results, last_successful_solver, last_successful_status


def _log_phase_summary(phase_results: list[OptimizationPhaseResult]) -> None:
    """最適化フェーズごとの到達状態と、最適性未証明の一覧を記録する。"""

    logger.info("optimization phase summary")
    for phase in phase_results:
        logger.info(
            "optimization phase name=%s status=%s optimal=%s objective_value=%s",
            phase.name,
            phase.status,
            phase.optimal,
            phase.objective_value,
        )
    logger.info(
        "optimization non_optimal_phases=%s",
        [phase.name for phase in phase_results if not phase.optimal],
    )


def _new_solver(max_time_seconds: int) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max_time_seconds
    solver.parameters.num_search_workers = int(
        os.environ.get("ORTOOLS_NUM_SEARCH_WORKERS", "8")
    )
    return solver


def _solve_and_fix_objective(
    *, model, objective, phase_name, max_time_seconds
) -> OptimizationPhaseResult:
    """1フェーズを解き、得られた最良値を後続フェーズ用の等式にする。"""

    model.Minimize(objective)
    solver = _new_solver(max_time_seconds)
    logger.info(
    "shift optimization phase started phase=%s limit=%ss",
    phase_name,
    max_time_seconds,
)
    status = solver.Solve(model)
    status_name = solver.StatusName(status)
    logger.info(
        "shift optimization phase=%s status=%s elapsed=%.3fs limit=%ss",
        phase_name,
        status_name,
        solver.WallTime(),
        max_time_seconds,
    )
    if status == cp_model.UNKNOWN:
        model.ClearObjective()
        return OptimizationPhaseResult(
            name=phase_name,
            status=status_name,
            objective_value=None,
            optimal=False,
            solver=None,
        )
    if status == cp_model.INFEASIBLE:
        raise InfeasibleOptimizationError()
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise OptimizationError(_build_solver_error_message(status_name))
    objective_value = int(round(solver.ObjectiveValue()))
    model.Add(objective == objective_value)
    model.ClearObjective()
    return OptimizationPhaseResult(
        name=phase_name,
        status=status_name,
        objective_value=objective_value,
        optimal=status == cp_model.OPTIMAL,
        solver=solver,
    )


def _build_shift_variables(*, model, staff_members, month_dates, fixed_assignments):
    """スタッフ×日付×勤務区分の BoolVar を作り、固定セルの基本制約も入れる。"""

    shift_vars = {}

    for staff_member in staff_members:
        for index, target_date in enumerate(month_dates):
            cell_key = (staff_member.id, target_date)
            day_vars = {
                shift_type: model.NewBoolVar(
                    f"shift_{staff_member.id}_{target_date.isoformat()}_{shift_type}"
                )
                for shift_type in GENERATABLE_SHIFT_TYPES
            }
            shift_vars[cell_key] = day_vars

            fixed_shift_type = fixed_assignments.get(cell_key)
            if fixed_shift_type is None:
                model.Add(sum(day_vars.values()) == 1)
            elif fixed_shift_type in GENERATABLE_SHIFT_TYPES:
                for shift_type, shift_var in day_vars.items():
                    model.Add(shift_var == int(shift_type == fixed_shift_type))
            else:
                # TRAINING / 有給 / 特別休暇 などは固定済みなので、生成対象の4区分はすべて 0 にする。
                model.Add(sum(day_vars.values()) == 0)

            if index == 0 and fixed_shift_type != SHIFT_AFTER_NIGHT:
                model.Add(day_vars[SHIFT_AFTER_NIGHT] == 0)

    return shift_vars


def _add_night_shift_eligibility_constraints(*, model, staff_members, month_dates, shift_vars):
    """夜勤不可スタッフには NIGHT を割り当てない。"""

    for staff_member in staff_members:
        if staff_member.can_night_shift:
            continue
        for target_date in month_dates:
            model.Add(
                shift_vars[(staff_member.id, target_date)][SHIFT_NIGHT] == 0
            )


def _add_next_month_first_regular_day_off_constraints(
    *, model, staff_members, month_dates, shift_vars
):
    """翌月1日が曜日固定休なら、当月末夜勤を候補から外す。"""
    next_month_first = month_dates[-1].fromordinal(month_dates[-1].toordinal() + 1)
    last_date = month_dates[-1]
    for staff_member in staff_members:
        regular_days = {
            day_off for day_off in staff_member.regular_days_off
        }
        if next_month_first.weekday() in regular_days:
            model.Add(
                shift_vars[(staff_member.id, last_date)][SHIFT_NIGHT]
                == 0
            )


def _add_night_pattern_constraints(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    fixed_assignments,
    effective_rules,
    user_override_assignment_keys=frozenset(),
):
    """夜勤後の勤務を連動させ、False時の明け翌日夜勤を月1回に制限する。

    False時に作成したパターン変数は、既存フェーズで回数を最小化するため返す。
    """

    night_after_night_pattern_terms = []

    for staff_member in staff_members:
        staff_pattern_terms = []
        first_date = month_dates[0]
        first_key = (staff_member.id, first_date)
        if (
            len(month_dates) >= 2
            and fixed_assignments.get(first_key)
            == SHIFT_AFTER_NIGHT
            and not effective_rules[first_date].night_shift_next_day_off
        ):
            second_date = month_dates[1]
            second_key = (staff_member.id, second_date)
            first_after_night_var = shift_vars[first_key][
                SHIFT_AFTER_NIGHT
            ]
            second_fixed_shift_type = fixed_assignments.get(second_key)
            if second_fixed_shift_type is not None:
                if second_fixed_shift_type not in (
                    OFF_LIKE_SHIFT_TYPES
                    | {SHIFT_NIGHT}
                ):
                    model.Add(first_after_night_var == 0)
            else:
                model.Add(
                    first_after_night_var
                    <= shift_vars[second_key][SHIFT_OFF]
                    + shift_vars[second_key][SHIFT_NIGHT]
                )

            second_night_var = shift_vars[second_key][
                SHIFT_NIGHT
            ]
            boundary_pattern_var = model.NewBoolVar(
                f"night_after_night_{staff_member.id}_month_boundary"
            )
            model.Add(boundary_pattern_var <= first_after_night_var)
            model.Add(boundary_pattern_var <= second_night_var)
            model.Add(
                boundary_pattern_var
                >= first_after_night_var + second_night_var - 1
            )
            staff_pattern_terms.append(boundary_pattern_var)

        for index, target_date in enumerate(month_dates):
            current_key = (staff_member.id, target_date)
            night_var = shift_vars[current_key][SHIFT_NIGHT]
            fixed_shift_type = fixed_assignments.get(current_key)

            if index + 1 >= len(month_dates):
                continue

            next_date = month_dates[index + 1]
            next_key = (staff_member.id, next_date)
            next_after_night_var = shift_vars[next_key][SHIFT_AFTER_NIGHT]
            model.Add(night_var == next_after_night_var)

            if index + 2 >= len(month_dates):
                continue

            third_date = month_dates[index + 2]
            third_key = (staff_member.id, third_date)
            third_fixed_shift_type = fixed_assignments.get(third_key)
            night_sequence_keys = (
                current_key,
                next_key,
                third_key,
            )
            is_manual_only_night_sequence = all(
                key in user_override_assignment_keys
                for key in night_sequence_keys
            )
            rule = effective_rules[target_date]

            if rule.night_shift_next_day_off:
                if third_fixed_shift_type == SHIFT_OFF_REQUEST:
                    continue
                if third_fixed_shift_type is not None:
                    if (
                        third_fixed_shift_type != SHIFT_OFF
                        and not is_manual_only_night_sequence
                    ):
                        model.Add(night_var == 0)
                    continue
                model.Add(
                    night_var
                    <= shift_vars[third_key][SHIFT_OFF]
                )
                continue

            if third_fixed_shift_type is not None:
                if third_fixed_shift_type not in (
                    OFF_LIKE_SHIFT_TYPES
                    | {SHIFT_NIGHT}
                ):
                    model.Add(night_var == 0)
            else:
                model.Add(
                    night_var
                    <= shift_vars[third_key][SHIFT_OFF]
                    + shift_vars[third_key][SHIFT_NIGHT]
                )

            third_night_var = shift_vars[third_key][
                SHIFT_NIGHT
            ]
            pattern_var = model.NewBoolVar(
                f"night_after_night_{staff_member.id}_{target_date.isoformat()}"
            )
            model.Add(pattern_var <= night_var)
            model.Add(pattern_var <= third_night_var)
            model.Add(pattern_var >= night_var + third_night_var - 1)
            staff_pattern_terms.append(pattern_var)

        if staff_pattern_terms:
            model.Add(sum(staff_pattern_terms) <= 1)
            night_after_night_pattern_terms.extend(staff_pattern_terms)

    return night_after_night_pattern_terms


def _add_monthly_off_day_constraints(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    fixed_assignments,
    effective_off_days,
):
    """月休日数を OFF / OFF_REQUEST の合計でぴったり一致させる。"""

    for staff_member in staff_members:
        fixed_non_generated_off_count = sum(
            1
            for target_date in month_dates
            if fixed_assignments.get((staff_member.id, target_date))
            in FIXED_NON_GENERATED_MONTHLY_OFF_SHIFT_TYPES
        )
        mandatory_off_count = sum(
            1
            for target_date in month_dates
            if fixed_assignments.get((staff_member.id, target_date))
            in MONTHLY_OFF_SHIFT_TYPES
        )
        model.Add(
            sum(
                shift_vars[(staff_member.id, target_date)][SHIFT_OFF]
                for target_date in month_dates
            )
            + fixed_non_generated_off_count
            == max(effective_off_days[staff_member.id], mandatory_off_count)
        )


def _add_required_night_staff_constraints(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    effective_rules,
) -> None:
    """日ごとの必要夜勤人数を必須制約として追加する。"""

    for target_date in month_dates:
        actual_night_staff = sum(
            shift_vars[(staff_member.id, target_date)][
                SHIFT_NIGHT
            ]
            for staff_member in staff_members
        )
        model.Add(
            actual_night_staff
            == effective_rules[target_date].required_night_staff
        )


def _build_day_staffing_balance_data(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    effective_rules,
    fixed_assignments,
    effective_off_days,
) -> DayStaffingBalanceData:
    """月間の日勤予定セル総数を、日別へ優先順に配分する。"""

    month_dates = list(month_dates)
    staff_members = list(staff_members)
    max_count = len(staff_members)
    total_planned_day_cells = _calculate_total_planned_day_cells(
        staff_members=staff_members,
        month_dates=month_dates,
        fixed_assignments=fixed_assignments,
        effective_rules=effective_rules,
        effective_off_days=effective_off_days,
    )
    day_count = len(month_dates)
    required_day_counts = {
        target_date: effective_rules[target_date].required_day_staff
        for target_date in month_dates
    }
    day_staffing_overrides = {
        target_date: effective_rules[target_date].required_day_staff_override
        for target_date in month_dates
        if effective_rules[target_date].required_day_staff_override is not None
    }
    initial_floor_target = total_planned_day_cells // day_count if day_count else 0
    initial_ceil_target = (
        math.ceil(total_planned_day_cells / day_count) if day_count else 0
    )
    high_day_staffing_overrides = {
        target_date: override
        for target_date, override in day_staffing_overrides.items()
        if override > initial_ceil_target
    }
    reserved_high_override_cells = sum(high_day_staffing_overrides.values())
    normal_dates = tuple(
        target_date
        for target_date in month_dates
        if target_date not in high_day_staffing_overrides
    )
    normal_day_cells = max(
        total_planned_day_cells - reserved_high_override_cells,
        0,
    )
    normal_day_count = len(normal_dates)
    normal_floor_target = (
        normal_day_cells // normal_day_count
        if normal_day_count
        else 0
    )
    normal_ceil_target = (
        math.ceil(normal_day_cells / normal_day_count)
        if normal_day_count
        else 0
    )
    normal_extra_cells = normal_day_cells % normal_day_count if normal_day_count else 0
    data = DayStaffingBalanceData(
        required_day_counts=required_day_counts,
        total_planned_day_cells=total_planned_day_cells,
        day_count=day_count,
        high_day_staffing_overrides=high_day_staffing_overrides,
        reserved_high_override_cells=reserved_high_override_cells,
        normal_day_cells=normal_day_cells,
        normal_dates=normal_dates,
        normal_floor_target=normal_floor_target,
        normal_ceil_target=normal_ceil_target,
        normal_extra_cells=normal_extra_cells,
    )

    logger.info(
        "day staffing calculation total_planned_day_cells=%s day_count=%s "
        "initial_targets=%s-%s day_staffing_overrides=%s "
        "high_override_dates=%s reserved_high_override_cells=%s "
        "normal_day_cells=%s normal_day_count=%s "
        "normal_targets=%s-%s normal_extra_cells=%s",
        total_planned_day_cells,
        day_count,
        initial_floor_target,
        initial_ceil_target,
        {
            target_date.isoformat(): override
            for target_date, override in day_staffing_overrides.items()
        },
        {
            target_date.isoformat(): override
            for target_date, override in high_day_staffing_overrides.items()
        },
        reserved_high_override_cells,
        normal_day_cells,
        normal_day_count,
        normal_floor_target,
        normal_ceil_target,
        normal_extra_cells,
    )

    for target_date in month_dates:
        actual_day_count = model.NewIntVar(
            0, max_count, f"actual_day_count_{target_date.isoformat()}"
        )
        model.Add(
            actual_day_count
            == sum(
                shift_vars[(staff_member.id, target_date)][
                    SHIFT_DAY
                ]
                for staff_member in staff_members
            )
        )
        required_day_count = required_day_counts[target_date]
        delta_lower_bound = -required_day_count
        delta_upper_bound = max_count - required_day_count
        delta_var = model.NewIntVar(
            delta_lower_bound,
            delta_upper_bound,
            f"day_staffing_delta_{target_date.isoformat()}",
        )
        model.Add(delta_var == actual_day_count - required_day_count)

        data.actual_day_count_vars[target_date] = actual_day_count
        data.day_staffing_delta_vars[target_date] = delta_var

    data.total_actual_day_count = sum(data.actual_day_count_vars.values())
    model.Add(
        data.total_actual_day_count == data.total_planned_day_cells
    )

    high_deviation_upper_bound = max(
        max_count,
        max(high_day_staffing_overrides.values(), default=0),
    )
    for target_date, override in high_day_staffing_overrides.items():
        deviation = model.NewIntVar(
            0,
            high_deviation_upper_bound,
            f"high_day_staffing_override_deviation_{target_date.isoformat()}",
        )
        model.AddAbsEquality(
            deviation,
            data.actual_day_count_vars[target_date] - override,
        )
        data.high_override_deviation_vars[target_date] = deviation
    data.maximum_high_override_deviation = _add_max_or_zero(
        model,
        list(data.high_override_deviation_vars.values()),
        high_deviation_upper_bound,
        "maximum_high_day_staffing_override_deviation",
    )
    total_high_deviation_upper_bound = (
        len(high_day_staffing_overrides) * high_deviation_upper_bound
    )
    data.total_high_override_deviation = model.NewIntVar(
        0,
        total_high_deviation_upper_bound,
        "total_high_day_staffing_override_deviation",
    )
    model.Add(
        data.total_high_override_deviation
        == sum(data.high_override_deviation_vars.values())
    )

    normal_target_band_deviation_terms = []
    low_override_ceil_penalty_terms = []
    if normal_dates:
        normal_minimum = model.NewIntVar(
            0, max_count, "normal_day_count_minimum"
        )
        normal_maximum = model.NewIntVar(
            0, max_count, "normal_day_count_maximum"
        )
        normal_actual_counts = [
            data.actual_day_count_vars[target_date]
            for target_date in normal_dates
        ]
        model.AddMinEquality(normal_minimum, normal_actual_counts)
        model.AddMaxEquality(normal_maximum, normal_actual_counts)
        data.normal_day_count_range = model.NewIntVar(
            0, max_count, "normal_day_count_range"
        )
        model.Add(
            data.normal_day_count_range == normal_maximum - normal_minimum
        )

        for target_date in normal_dates:
            below_floor = model.NewIntVar(
                0,
                max_count,
                f"normal_day_below_floor_{target_date.isoformat()}",
            )
            model.AddMaxEquality(
                below_floor,
                [
                    normal_floor_target
                    - data.actual_day_count_vars[target_date],
                    0,
                ],
            )
            above_ceil = model.NewIntVar(
                0,
                max_count,
                f"normal_day_above_ceil_{target_date.isoformat()}",
            )
            model.AddMaxEquality(
                above_ceil,
                [
                    data.actual_day_count_vars[target_date]
                    - normal_ceil_target,
                    0,
                ],
            )
            normal_target_band_deviation_terms.extend((below_floor, above_ceil))

            override = day_staffing_overrides.get(target_date)
            if override is not None and override < normal_floor_target:
                ceil_side_count = model.NewIntVar(
                    0,
                    max_count,
                    f"low_override_ceil_penalty_{target_date.isoformat()}",
                )
                model.AddMaxEquality(
                    ceil_side_count,
                    [
                        data.actual_day_count_vars[target_date]
                        - normal_floor_target,
                        0,
                    ],
                )
                low_override_ceil_penalty_terms.append(ceil_side_count)
    else:
        data.normal_day_count_range = 0

    normal_target_band_deviation_upper_bound = 2 * len(normal_dates) * max_count
    if normal_target_band_deviation_terms:
        data.normal_target_band_deviation = model.NewIntVar(
            0,
            normal_target_band_deviation_upper_bound,
            "normal_day_target_band_deviation",
        )
        model.Add(
            data.normal_target_band_deviation
            == sum(normal_target_band_deviation_terms)
        )
    else:
        data.normal_target_band_deviation = 0

    low_override_ceil_penalty_upper_bound = (
        len(low_override_ceil_penalty_terms) * max_count
    )
    if low_override_ceil_penalty_terms:
        data.low_override_ceil_penalty = model.NewIntVar(
            0,
            low_override_ceil_penalty_upper_bound,
            "low_override_ceil_penalty",
        )
        model.Add(
            data.low_override_ceil_penalty
            == sum(low_override_ceil_penalty_terms)
        )
    else:
        data.low_override_ceil_penalty = 0

    data.minimum_actual_day_count = model.NewIntVar(
        0,
        max_count,
        "minimum_actual_day_count",
    )
    data.maximum_actual_day_count = model.NewIntVar(
        0,
        max_count,
        "maximum_actual_day_count",
    )
    model.AddMinEquality(
        data.minimum_actual_day_count,
        list(data.actual_day_count_vars.values()),
    )
    model.AddMaxEquality(
        data.maximum_actual_day_count,
        list(data.actual_day_count_vars.values()),
    )
    data.actual_day_count_range = model.NewIntVar(
        0,
        max_count,
        "actual_day_count_range",
    )
    model.Add(
        data.actual_day_count_range
        == data.maximum_actual_day_count - data.minimum_actual_day_count
    )
    data.objective_score = _build_lexicographic_score(
        [
            (
                data.maximum_high_override_deviation,
                high_deviation_upper_bound,
            ),
            (
                data.total_high_override_deviation,
                total_high_deviation_upper_bound,
            ),
            (data.normal_day_count_range, max_count),
            (
                data.normal_target_band_deviation,
                normal_target_band_deviation_upper_bound,
            ),
            (
                data.low_override_ceil_penalty,
                low_override_ceil_penalty_upper_bound,
            ),
        ]
    )
    return data


def _calculate_total_planned_day_cells(
    *,
    staff_members,
    month_dates,
    fixed_assignments,
    effective_rules,
    effective_off_days,
) -> int:
    """月休日・夜勤・明け・固定の非日勤セルを除いた日勤総数を返す。"""

    month_dates = list(month_dates)
    staff_members = list(staff_members)
    if not month_dates:
        return 0

    fixed_non_day_cells = sum(
        1
        for staff_member in staff_members
        for target_date in month_dates
        if (
            (
                fixed_shift_type := fixed_assignments.get(
                    (staff_member.id, target_date)
                )
            )
            is not None
            and fixed_shift_type not in GENERATABLE_SHIFT_TYPES
            and fixed_shift_type != SHIFT_OFF_REQUEST
        )
    )
    monthly_off_cells = sum(
        max(
            effective_off_days[staff_member.id],
            sum(
                fixed_assignments.get((staff_member.id, target_date))
                in MONTHLY_OFF_SHIFT_TYPES
                for target_date in month_dates
            ),
        )
        for staff_member in staff_members
    )
    required_night_cells = sum(
        effective_rules[target_date].required_night_staff
        for target_date in month_dates
    )
    first_date = month_dates[0]
    boundary_after_night_cells = sum(
        fixed_assignments.get((staff_member.id, first_date))
        == SHIFT_AFTER_NIGHT
        for staff_member in staff_members
    )
    in_month_after_night_cells = sum(
        effective_rules[target_date].required_night_staff
        for target_date in month_dates[:-1]
    )
    return (
        len(staff_members) * len(month_dates)
        - fixed_non_day_cells
        - monthly_off_cells
        - required_night_cells
        - boundary_after_night_cells
        - in_month_after_night_cells
    )


def _add_staffing_safety_constraints(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    effective_rules,
) -> None:
    """日ごとのリーダー人数・能力条件を必須制約として追加する。"""

    leaders = [
        staff
        for staff in staff_members
        if staff.role == ROLE_LEADER
    ]
    for target_date in month_dates:
        rule = effective_rules[target_date]
        actual_leaders = sum(
            shift_vars[(staff.id, target_date)][
                SHIFT_DAY
            ]
            for staff in leaders
        )
        model.Add(actual_leaders >= rule.required_leader_staff)

        if (
            rule.min_ability_level is None
            or rule.min_ability_level_staff_count is None
        ):
            continue
        actual_qualified_staff = sum(
            shift_vars[(staff.id, target_date)][
                SHIFT_DAY
            ]
            for staff in staff_members
            if staff.ability_level >= rule.min_ability_level
        )
        model.Add(
            actual_qualified_staff >= rule.min_ability_level_staff_count
        )


def _add_max_consecutive_work_constraints(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    fixed_assignments,
    max_consecutive_work_days,
    previous_consecutive_work_days,
    user_override_assignment_keys=frozenset(),
) -> None:
    """ユーザー操作だけの既存連勤を除き、最大連勤を超える配置を禁止する。"""

    window_size = max_consecutive_work_days + 1
    for staff_member in staff_members:
        prefix_count = min(
            previous_consecutive_work_days.get(staff_member.id, 0),
            max_consecutive_work_days,
        )
        timeline = [None] * prefix_count + month_dates
        for start_index in range(0, len(timeline) - window_size + 1):
            window = timeline[start_index : start_index + window_size]
            current_month_dates = [
                current_date for current_date in window if current_date is not None
            ]
            if current_month_dates and all(
                (staff_member.id, current_date)
                in user_override_assignment_keys
                for current_date in current_month_dates
            ):
                # 手入力だけで既に超過している連勤は保持する。後続に自動生成
                # セルが含まれる次のウィンドウでは通常どおり休みを強制する。
                continue
            work_terms = [
                1
                if current_date is None
                else _work_term(
                    shift_vars,
                    fixed_assignments,
                    staff_member.id,
                    current_date,
                )
                for current_date in window
            ]

            model.Add(sum(work_terms) <= max_consecutive_work_days)


def _work_term(shift_vars, fixed_assignments, staff_member_id, target_date):
    """最大連勤制約と長期連勤評価で同じ勤務日定義を使用する。"""
    cell_key = (staff_member_id, target_date)
    fixed_shift_type = fixed_assignments.get(cell_key)
    if fixed_shift_type is not None:
        return int(fixed_shift_type in WORKLIKE_SHIFT_TYPES)
    return (
        shift_vars[cell_key][SHIFT_DAY]
        + shift_vars[cell_key][SHIFT_NIGHT]
        + shift_vars[cell_key][SHIFT_AFTER_NIGHT]
    )


def _build_night_count_balance_objective(
    *,
    model,
    staff_members,
    month_dates,
    shift_vars,
    night_after_night_pattern_terms=(),
    night_ability_balance_data: NightAbilityBalanceData,
) -> NightCountBalanceData:
    """夜勤回数、明け翌日夜勤、夜勤能力合計の順に最適化する。"""

    data = NightCountBalanceData()
    pattern_penalty = (
        sum(night_after_night_pattern_terms)
        if night_after_night_pattern_terms
        else 0
    )
    eligible_staff = [staff for staff in staff_members if staff.can_night_shift]
    if len(eligible_staff) <= 1:
        data.night_balance_violation = 0
    else:
        for staff_member in eligible_staff:
            count_var = model.NewIntVar(
                0,
                len(month_dates),
                f"night_count_{staff_member.id}",
            )
            model.Add(
                count_var
                == sum(
                    shift_vars[(staff_member.id, target_date)][
                        SHIFT_NIGHT
                    ]
                    for target_date in month_dates
                )
            )
            data.night_count_vars[staff_member.id] = count_var
        data.night_count_min = model.NewIntVar(
            0, len(month_dates), "night_count_min"
        )
        data.night_count_max = model.NewIntVar(
            0, len(month_dates), "night_count_max"
        )
        model.AddMinEquality(
            data.night_count_min, list(data.night_count_vars.values())
        )
        model.AddMaxEquality(
            data.night_count_max, list(data.night_count_vars.values())
        )
        model.Add(
            data.night_count_max - data.night_count_min <= 2
        )
        data.night_balance_violation = model.NewIntVar(
            0, len(month_dates), "night_balance_violation"
        )
        model.Add(
            data.night_balance_violation
            >= data.night_count_max - data.night_count_min - 1
        )

    ability_deviation_upper_bound = _night_ability_deviation_upper_bound(
        night_ability_balance_data
    )
    data.objective_score = _build_lexicographic_score(
        [
            (data.night_balance_violation, len(month_dates)),
            (pattern_penalty, len(staff_members)),
            (
                night_ability_balance_data.max_deviation,
                ability_deviation_upper_bound,
            ),
            (
                night_ability_balance_data.total_deviation,
                len(month_dates) * ability_deviation_upper_bound,
            ),
        ]
    )
    return data


def _add_long_consecutive_work_objective(
    *, model, staff_members, month_dates, shift_vars, fixed_assignments,
    max_consecutive_work_days, previous_consecutive_work_days,
):
    terms = []
    thresholds = []
    if max_consecutive_work_days >= 2:
        thresholds.append((max_consecutive_work_days - 1, LONG_STREAK_WEIGHTS["near_max"]))
    thresholds.append((max_consecutive_work_days, LONG_STREAK_WEIGHTS["at_max"]))

    for staff_member in staff_members:
        prefix_count = min(
            previous_consecutive_work_days.get(staff_member.id, 0),
            max_consecutive_work_days,
        )
        timeline = [None] * prefix_count + month_dates
        for length, length_weight in thresholds:
            for start_index in range(len(timeline) - length + 1):
                window = timeline[start_index:start_index + length]
                work_terms = [
                    1 if target_date is None else _work_term(
                        shift_vars, fixed_assignments, staff_member.id, target_date
                    )
                    for target_date in window
                ]
                streak = model.NewBoolVar(
                    f"long_streak_{staff_member.id}_{length}_{start_index}"
                )
                # AND と同値。固定勤務も式へ含まれる。
                for work_term in work_terms:
                    model.Add(streak <= work_term)
                model.Add(streak >= sum(work_terms) - length + 1)
                terms.append(streak * length_weight)
    return terms


def _build_lexicographic_score(components):
    """(式, 上限)を優先順に並べ、安全な係数の整数スコアへ変換する。"""

    score_terms = []
    multiplier = 1
    score_upper_bound = 0
    for expression, upper_bound in reversed(components):
        score_upper_bound += upper_bound * multiplier
        if score_upper_bound > CP_SAT_INT_MAX:
            raise OptimizationError(
                "最適化スコアがOR-Toolsの整数上限を超えるため生成できません。"
            )
        score_terms.append(expression * multiplier)
        multiplier *= upper_bound + 1
    return sum(score_terms)


def _build_day_ability_balance_objective(
    *, model, month_dates, shift_vars, staff_members, actual_day_count_vars
) -> DayAbilityBalanceData:
    """日勤のLv1〜5構成比と能力合計を、全スタッフ母集団へ近づける。"""

    month_dates = list(month_dates)
    staff_members = list(staff_members)
    staff_count = len(staff_members)
    data = DayAbilityBalanceData(
        staff_count=staff_count,
        staff_level_counts={
            level: sum(staff.ability_level == level for staff in staff_members)
            for level in ABILITY_LEVELS
        },
        total_staff_ability=sum(staff.ability_level for staff in staff_members),
        actual_day_count_vars=dict(actual_day_count_vars),
    )
    level_upper_bounds = {
        level: staff_count * count
        for level, count in data.staff_level_counts.items()
    }
    total_upper_bound = staff_count * data.total_staff_ability

    for target_date in month_dates:
        actual_day_count = data.actual_day_count_vars[target_date]
        for level in ABILITY_LEVELS:
            level_count = model.NewIntVar(
                0,
                data.staff_level_counts[level],
                f"day_ability_level_{level}_count_{target_date.isoformat()}",
            )
            model.Add(
                level_count
                == sum(
                    shift_vars[(staff.id, target_date)][SHIFT_DAY]
                    for staff in staff_members
                    if staff.ability_level == level
                )
            )
            key = (target_date, level)
            data.level_count_vars[key] = level_count
            deviation = model.NewIntVar(
                0,
                level_upper_bounds[level],
                f"day_ability_level_{level}_deviation_{target_date.isoformat()}",
            )
            model.AddAbsEquality(
                deviation,
                level_count * staff_count
                - actual_day_count * data.staff_level_counts[level],
            )
            data.level_deviation_vars[key] = deviation

        ability_total = model.NewIntVar(
            0, data.total_staff_ability,
            f"day_ability_total_{target_date.isoformat()}",
        )
        model.Add(
            ability_total
            == sum(
                shift_vars[(staff.id, target_date)][SHIFT_DAY]
                * staff.ability_level
                for staff in staff_members
            )
        )
        data.daily_ability_total_vars[target_date] = ability_total
        deviation = model.NewIntVar(
            0, total_upper_bound,
            f"day_ability_total_deviation_{target_date.isoformat()}",
        )
        model.AddAbsEquality(
            deviation,
            ability_total * staff_count
            - actual_day_count * data.total_staff_ability,
        )
        data.ability_total_deviation_vars[target_date] = deviation

    max_level_upper_bound = max(level_upper_bounds.values(), default=0)
    total_level_upper_bound = len(month_dates) * sum(level_upper_bounds.values())
    data.max_level_deviation = _add_max_or_zero(
        model, list(data.level_deviation_vars.values()),
        max_level_upper_bound, "day_ability_max_level_deviation",
    )
    data.total_level_deviation = model.NewIntVar(
        0, total_level_upper_bound, "day_ability_total_level_deviation"
    )
    model.Add(data.total_level_deviation == sum(data.level_deviation_vars.values()))
    data.max_ability_total_deviation = _add_max_or_zero(
        model, list(data.ability_total_deviation_vars.values()),
        total_upper_bound, "day_ability_max_total_deviation",
    )
    total_ability_upper_bound = len(month_dates) * total_upper_bound
    data.total_ability_total_deviation = model.NewIntVar(
        0, total_ability_upper_bound, "day_ability_total_total_deviation"
    )
    model.Add(
        data.total_ability_total_deviation
        == sum(data.ability_total_deviation_vars.values())
    )
    data.objective_score = _build_lexicographic_score(
        [
            (data.max_level_deviation, max_level_upper_bound),
            (data.total_level_deviation, total_level_upper_bound),
            (data.max_ability_total_deviation, total_upper_bound),
            (data.total_ability_total_deviation, total_ability_upper_bound),
        ]
    )
    return data


def _build_night_ability_balance_objective(
    *, model, month_dates, shift_vars, effective_rules, night_eligible_staff
) -> NightAbilityBalanceData:
    """夜勤可能スタッフの平均能力へ各日の夜勤能力合計を近づける。"""

    month_dates = list(month_dates)
    night_eligible_staff = list(night_eligible_staff)
    data = NightAbilityBalanceData(
        eligible_staff_count=len(night_eligible_staff),
        eligible_staff_ability_total=sum(
            staff.ability_level for staff in night_eligible_staff
        ),
        required_night_counts={
            target_date: effective_rules[target_date].required_night_staff
            for target_date in month_dates
        },
    )
    deviation_upper_bound = _night_ability_deviation_upper_bound(data)
    for target_date in month_dates:
        ability_total = model.NewIntVar(
            0, data.eligible_staff_ability_total,
            f"night_ability_total_{target_date.isoformat()}",
        )
        model.Add(
            ability_total
            == sum(
                shift_vars[(staff.id, target_date)][SHIFT_NIGHT]
                * staff.ability_level
                for staff in night_eligible_staff
            )
        )
        data.daily_ability_total_vars[target_date] = ability_total
        deviation = model.NewIntVar(
            0, deviation_upper_bound,
            f"night_ability_deviation_{target_date.isoformat()}",
        )
        model.AddAbsEquality(
            deviation,
            ability_total * data.eligible_staff_count
            - data.required_night_counts[target_date]
            * data.eligible_staff_ability_total,
        )
        data.deviation_vars[target_date] = deviation

    data.max_deviation = _add_max_or_zero(
        model, list(data.deviation_vars.values()),
        deviation_upper_bound, "night_ability_max_deviation",
    )
    total_upper_bound = len(month_dates) * deviation_upper_bound
    data.total_deviation = model.NewIntVar(
        0, total_upper_bound, "night_ability_total_deviation"
    )
    model.Add(data.total_deviation == sum(data.deviation_vars.values()))
    data.objective_score = _build_lexicographic_score(
        [
            (data.max_deviation, deviation_upper_bound),
            (data.total_deviation, total_upper_bound),
        ]
    )
    return data


def _night_ability_deviation_upper_bound(data: NightAbilityBalanceData) -> int:
    """夜勤能力交差積偏差の安全な上限を返す。"""

    maximum_required_count = max(data.required_night_counts.values(), default=0)
    return max(
        data.eligible_staff_count * data.eligible_staff_ability_total,
        maximum_required_count * data.eligible_staff_ability_total,
    )


def _add_max_or_zero(model, variables, upper_bound, name):
    if not variables:
        return 0
    maximum = model.NewIntVar(0, upper_bound, name)
    model.AddMaxEquality(maximum, variables)
    return maximum


def _build_solver_error_message(solver_status):
    """OR-Tools の終了状態を、画面向けに短いエラーメッセージへ変換する。"""

    if solver_status == "INFEASIBLE":
        return "固定条件が競合しているため、自動生成できませんでした。"
    if solver_status == "MODEL_INVALID":
        return "シフト生成モデルが不正な状態です。条件設定を確認してください。"
    if solver_status == "UNKNOWN":
        return "制限時間内に解を見つけられませんでした。条件を見直して再実行してください。"
    return f"シフトを自動生成できませんでした。（solver_status={solver_status}）"
