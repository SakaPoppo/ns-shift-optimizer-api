"""OR-Toolsの内部結果を、JSONで返せるAPIレスポンスへ変換する。

SolverやBoolVar、日付タプルキーをそのまま外部へ出さず、勤務一覧・フェーズ結果・
機械可読なGenerationIssueへ変換する。画面用の日本語文言はDjango側の責務である。
"""

from collections import Counter
from datetime import date

from .constants import (
    GENERATABLE_SHIFT_TYPES,
    MONTHLY_OFF_SHIFT_TYPES,
    ROLE_LEADER,
    SHIFT_DAY,
    SHIFT_NIGHT,
)
from .schemas import (
    GenerationIssueResponse,
    GenerateShiftResponse,
    GeneratedShiftOutput,
    OptimizationPhaseOutput,
)
from .types import OptimizationContext, OptimizationError, ShiftOptimizationOutput


ABILITY_DEVIATION_WARNING_PERCENT = 25
PERCENT_SCALE = 100


def build_generate_shift_response(
    *,
    context: OptimizationContext,
    optimization: ShiftOptimizationOutput,
) -> GenerateShiftResponse:
    """CP-SAT内部オブジェクトを公開せず、生成成功レスポンスを組み立てる。"""

    shifts = []
    for staff_member in context.staff_members:
        for target_date in context.month_dates:
            cell_key = (staff_member.id, target_date)
            fixed_shift_type = context.fixed_assignments.get(cell_key)
            shift_type = fixed_shift_type or _selected_shift_type(
                optimization=optimization,
                cell_key=cell_key,
            )
            if shift_type is None:
                raise OptimizationError(
                    "勤務結果を取得できませんでした。条件設定を確認してください。"
                )
            shifts.append(
                GeneratedShiftOutput(
                    staff_id=staff_member.id,
                    date=target_date,
                    shift_type=shift_type,
                )
            )

    return GenerateShiftResponse(
        status="success",
        solver_status=optimization.solver_status,
        shifts=shifts,
        phase_results=[
            OptimizationPhaseOutput(
                name=phase.name,
                status=phase.status,
                objective_value=phase.objective_value,
                optimal=phase.optimal,
            )
            for phase in optimization.phase_results
        ],
        issues=_build_generation_issues(
            context=context,
            optimization=optimization,
            shifts=shifts,
        ),
    )


def build_infeasible_generate_shift_response(
    *, context: OptimizationContext
) -> GenerateShiftResponse:
    """制約が両立しない場合の、画面表示可能なレスポンスを返す。"""

    issues = _build_hard_staffing_issues(context)
    if not issues:
        issues.append(
            GenerationIssueResponse(
                code="GENERATION_INFEASIBLE",
                severity="error",
            )
        )
    return GenerateShiftResponse(
        status="infeasible",
        solver_status="INFEASIBLE",
        shifts=[],
        phase_results=[],
        issues=issues,
    )


def _selected_shift_type(*, optimization: ShiftOptimizationOutput, cell_key) -> str | None:
    """最終Solverが選択した、そのセルの勤務区分を1件だけ取り出す。"""

    day_vars = optimization.shift_vars[cell_key]
    selected_shift_types = [
        shift_type
        for shift_type in GENERATABLE_SHIFT_TYPES
        if optimization.solver.Value(day_vars[shift_type])
    ]
    if len(selected_shift_types) != 1:
        return None
    return selected_shift_types[0]


def _solver_value(solver, expression) -> int:
    return int(solver.Value(expression)) if expression is not None else 0


def _serialize_daily_counts(counts: dict[date, int]) -> dict[str, int]:
    """日付キーの集計値を、UI文言を混ぜずJSON安全な文字列キーへ変換する。"""

    return {target_date.isoformat(): count for target_date, count in counts.items()}


def _most_frequent_count(
    counts: dict,
) -> int | None:
    """最頻値を返す。同数なら小さい人数を採用する。"""

    frequencies = Counter(counts.values())
    if not frequencies:
        return None
    highest_frequency = max(frequencies.values())
    return min(
        count
        for count, frequency in frequencies.items()
        if frequency == highest_frequency
    )


def _build_generation_issues(
    *,
    context: OptimizationContext | None = None,
    optimization: ShiftOptimizationOutput,
    shifts: list[GeneratedShiftOutput] | None = None,
) -> list[GenerationIssueResponse]:
    """解けたモデルから、Djangoと共有する構造化Issueを作る。

APIは日付・人数・差分などの事実だけを返し、日本語の表示文言はDjango側で決める。
"""

    day_data = optimization.day_staffing_balance_data
    actual_day_counts = {
        target_date: _solver_value(optimization.solver, actual_count_var)
        for target_date, actual_count_var in day_data.actual_day_count_vars.items()
    }
    required_day_counts = dict(day_data.required_day_counts)
    day_staffing_deltas = {
        target_date: _solver_value(optimization.solver, delta_var)
        for target_date, delta_var in day_data.day_staffing_delta_vars.items()
    }
    daily_count_details = {
        "actual_day_counts": _serialize_daily_counts(actual_day_counts),
        "required_day_counts": _serialize_daily_counts(required_day_counts),
    }

    issues = [
        GenerationIssueResponse(
            code="SHIFT_GENERATED",
            severity="success",
        )
    ]
    above_dates = [
        target_date
        for target_date, delta in day_staffing_deltas.items()
        if delta > 0
    ]
    if above_dates:
        issues.append(
            GenerationIssueResponse(
                code="DAY_STAFFING_ABOVE_REQUIRED",
                severity="info",
                dates=above_dates,
                details=daily_count_details,
            )
        )

    below_dates = [
        target_date
        for target_date, delta in day_staffing_deltas.items()
        if delta < 0
    ]
    if below_dates:
        issues.append(
            GenerationIssueResponse(
                code="DAY_STAFFING_BELOW_REQUIRED",
                severity="warning",
                dates=below_dates,
                details=daily_count_details,
            )
        )

    modal_day_staffing_count = _most_frequent_count(
        actual_day_counts
    )
    imbalance_dates = (
        [
            target_date
            for target_date, actual_count in actual_day_counts.items()
            if abs(actual_count - modal_day_staffing_count) >= 2
        ]
        if modal_day_staffing_count is not None
        else []
    )
    if imbalance_dates:
        issues.append(
            GenerationIssueResponse(
                code="DAY_STAFFING_IMBALANCE",
                severity="warning",
                dates=imbalance_dates,
                details={
                    **daily_count_details,
                    "modal_day_staffing_count": modal_day_staffing_count,
                    "count_difference_threshold": 2,
                },
            )
        )

    night_counts = {
        staff_id: _solver_value(optimization.solver, count_var)
        for staff_id, count_var in (
            optimization.night_count_balance_data.night_count_vars.items()
        )
    }
    if len(night_counts) > 1:
        minimum_count = min(night_counts.values())
        maximum_count = max(night_counts.values())
        difference = maximum_count - minimum_count
        minimum_count_staff_ids = [
            staff_id
            for staff_id, count in night_counts.items()
            if count == minimum_count
        ]
        maximum_count_staff_ids = [
            staff_id
            for staff_id, count in night_counts.items()
            if count == maximum_count
        ]
        if difference >= 2:
            imbalanced_staff_ids = (
                minimum_count_staff_ids
                if len(minimum_count_staff_ids) <= len(maximum_count_staff_ids)
                else maximum_count_staff_ids
            )
            issues.append(
                GenerationIssueResponse(
                    code="NIGHT_COUNT_IMBALANCE",
                    severity="warning",
                    staff_ids=imbalanced_staff_ids,
                    details={
                        "minimum_count": minimum_count,
                        "maximum_count": maximum_count,
                        "count_difference": difference,
                        "count_difference_threshold": 2,
                        "alerted_count": night_counts[imbalanced_staff_ids[0]],
                    },
                )
            )

    if context is not None and shifts is not None:
        issues.extend(_build_ability_target_issues(context, shifts))

    incomplete_items = [
        phase.name
        for phase in optimization.phase_results
        if phase.status in {"UNKNOWN", "NOT_RUN"}
    ]
    if incomplete_items:
        issues.append(
            GenerationIssueResponse(
                code="OPTIMIZATION_INCOMPLETE",
                severity="warning",
                details={"incomplete_items": incomplete_items},
            )
        )
    if context is not None and shifts is not None:
        actual_monthly_off_counts = Counter(
            shift.staff_id
            for shift in shifts
            if shift.shift_type in MONTHLY_OFF_SHIFT_TYPES
        )
        for staff_member in context.staff_members:
            configured_off_count = context.configured_off_days[staff_member.id]
            actual_off_count = actual_monthly_off_counts[staff_member.id]
            if actual_off_count > configured_off_count:
                issues.append(
                    GenerationIssueResponse(
                        code="MONTHLY_OFF_COUNT_EXCEEDED",
                        severity="warning",
                        staff_ids=[staff_member.id],
                        details={
                            "configured_off_count": configured_off_count,
                            "actual_off_count": actual_off_count,
                            "excess_count": actual_off_count - configured_off_count,
                        },
                    )
                )
    return issues


def _build_ability_target_issues(
    context: OptimizationContext,
    shifts: list[GeneratedShiftOutput],
) -> list[GenerationIssueResponse]:
    """日別能力合計が期待値から25%以上ずれた場合に警告Issueを返す。"""

    staff_by_id = {staff.id: staff for staff in context.staff_members}
    night_eligible_staff = [
        staff for staff in context.staff_members if staff.can_night_shift
    ]
    shifts_by_date = {
        target_date: [
            shift for shift in shifts if shift.date == target_date
        ]
        for target_date in context.month_dates
    }
    issues = []
    for target_date in context.month_dates:
        day_shifts = [
            shift
            for shift in shifts_by_date[target_date]
            if shift.shift_type == SHIFT_DAY
        ]
        issues.extend(
            _build_daily_ability_target_issue(
                target_date=target_date,
                target_count=len(day_shifts),
                actual_ability_total=sum(
                    staff_by_id[shift.staff_id].ability_level
                    for shift in day_shifts
                ),
                population=context.staff_members,
                below_code="DAY_ABILITY_BELOW_TARGET",
                above_code="DAY_ABILITY_ABOVE_TARGET",
            )
        )

        night_shifts = [
            shift
            for shift in shifts_by_date[target_date]
            if shift.shift_type == SHIFT_NIGHT
            and staff_by_id[shift.staff_id].can_night_shift
        ]
        issues.extend(
            _build_daily_ability_target_issue(
                target_date=target_date,
                target_count=(
                    context.effective_rules[target_date].required_night_staff
                ),
                actual_ability_total=sum(
                    staff_by_id[shift.staff_id].ability_level
                    for shift in night_shifts
                ),
                population=night_eligible_staff,
                below_code="NIGHT_ABILITY_BELOW_TARGET",
                above_code="NIGHT_ABILITY_ABOVE_TARGET",
            )
        )
    return issues


def _build_daily_ability_target_issue(
    *,
    target_date: date,
    target_count: int,
    actual_ability_total: int,
    population,
    below_code: str,
    above_code: str,
) -> list[GenerationIssueResponse]:
    """浮動小数を避け、交差積で1日分の能力合計偏差を評価する。"""

    population_count = len(population)
    population_ability_total = sum(
        staff.ability_level for staff in population
    )
    expected_numerator = target_count * population_ability_total
    if population_count == 0 or expected_numerator == 0:
        return []

    actual_numerator = actual_ability_total * population_count
    deviation_numerator = abs(actual_numerator - expected_numerator)
    if (
        deviation_numerator * PERCENT_SCALE
        < expected_numerator * ABILITY_DEVIATION_WARNING_PERCENT
    ):
        return []

    return [
        GenerationIssueResponse(
            code=(
                below_code
                if actual_numerator < expected_numerator
                else above_code
            ),
            severity="warning",
            dates=[target_date],
            details={
                "date": target_date.isoformat(),
                "actual_ability_total": actual_ability_total,
                "expected_ability_total": (
                    expected_numerator / population_count
                ),
                "deviation_rate": (
                    deviation_numerator / expected_numerator
                ),
            },
        )
    ]


def _build_hard_staffing_issues(
    context: OptimizationContext,
) -> list[GenerationIssueResponse]:
    """固定勤務だけで確定する日別の配置不足をIssueとして返す。

Solverを解かなければ判断できない競合は推測せず、汎用INFEASIBLEへ委ねる。
"""

    issues = []
    for target_date in context.month_dates:
        rule = context.effective_rules[target_date]
        fixed_shift_by_staff_id = {
            staff_id: shift_type
            for (staff_id, assignment_date), shift_type in (
                context.fixed_assignments.items()
            )
            if assignment_date == target_date
        }
        available_day_staff = [
            staff
            for staff in context.staff_members
            if fixed_shift_by_staff_id.get(staff.id) in {None, SHIFT_DAY}
        ]
        available_leader_count = sum(
            staff.role == ROLE_LEADER for staff in available_day_staff
        )
        available_night_count = sum(
            staff.can_night_shift
            and fixed_shift_by_staff_id.get(staff.id) in {None, SHIFT_NIGHT}
            for staff in context.staff_members
        )
        if available_night_count < rule.required_night_staff:
            issues.append(
                GenerationIssueResponse(
                    code="INSUFFICIENT_NIGHT_STAFF",
                    severity="error",
                    dates=[target_date],
                    details={
                        "available_count": available_night_count,
                        "required_count": rule.required_night_staff,
                    },
                )
            )
        if available_leader_count < rule.required_leader_staff:
            issues.append(
                GenerationIssueResponse(
                    code="INSUFFICIENT_LEADER_STAFF",
                    severity="error",
                    dates=[target_date],
                    details={
                        "available_count": available_leader_count,
                        "required_count": rule.required_leader_staff,
                    },
                )
            )
    return issues
