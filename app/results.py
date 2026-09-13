"""Conversion from OR-Tools output to JSON-safe API response models."""

from .constants import GENERATABLE_SHIFT_TYPES
from .schemas import (
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
