"""Conversion from OR-Tools output to JSON-safe API response models."""

from datetime import date

from .constants import GENERATABLE_SHIFT_TYPES
from .schemas import (
    GenerationIssueResponse,
    GenerateShiftResponse,
    GeneratedShiftOutput,
    OptimizationPhaseOutput,
)
from .types import OptimizationContext, OptimizationError, ShiftOptimizationOutput


def build_generate_shift_response(
    *,
    context: OptimizationContext,
    optimization: ShiftOptimizationOutput,
) -> GenerateShiftResponse:
    """Build an API response without exposing CP-SAT objects or tuple keys."""

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
            optimization=optimization,
        ),
    )


def _selected_shift_type(*, optimization: ShiftOptimizationOutput, cell_key) -> str | None:
    """Return the single generated shift selected by the final solver."""

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
    """Keep date-keyed solver data JSON-safe without introducing UI text."""

    return {target_date.isoformat(): count for target_date, count in counts.items()}


def _build_generation_issues(
    *,
    optimization: ShiftOptimizationOutput,
) -> list[GenerationIssueResponse]:
    """Build the shared structured Issue contract from a solved model.

    This mirrors Django's local ``build_generation_issues`` criteria.  It is
    intentionally kept at the API response boundary so the API exposes facts,
    not Japanese UI messages.
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

    imbalance_dates = [
        target_date
        for target_date, delta in day_staffing_deltas.items()
        if abs(delta) >= 2
    ]
    if imbalance_dates:
        issues.append(
            GenerationIssueResponse(
                code="DAY_STAFFING_IMBALANCE",
                severity="warning",
                dates=imbalance_dates,
                details={
                    **daily_count_details,
                    "minimum_delta": _solver_value(
                        optimization.solver, day_data.minimum_delta
                    ),
                    "maximum_delta": _solver_value(
                        optimization.solver, day_data.maximum_delta
                    ),
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
        if difference > 1:
            issues.append(
                GenerationIssueResponse(
                    code="NIGHT_COUNT_IMBALANCE",
                    severity="warning",
                    staff_ids=[
                        staff_id
                        for staff_id, count in night_counts.items()
                        if count in {minimum_count, maximum_count}
                    ],
                    details={
                        "minimum_count": minimum_count,
                        "maximum_count": maximum_count,
                        "count_difference": difference,
                    },
                )
            )

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
    return issues
