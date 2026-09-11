"""HTTP request and response schemas for the optimizer boundary.

These models deliberately contain no Django models or database concerns.  They
mirror the JSON produced by Ns Shift's ``build_optimizer_payload`` function.
"""

from datetime import date

from pydantic import BaseModel

from .types import AbilityLevel, Weekday


class StaffInput(BaseModel):
    """A staff member available to the optimizer."""

    id: int
    role: str
    ability_level: AbilityLevel
    can_night_shift: bool
    regular_days_off: list[Weekday]


class FixedAssignmentInput(BaseModel):
    """A shift assignment that the optimizer must preserve."""

    staff_id: int
    date: date
    shift_type: str


class EffectiveRuleInput(BaseModel):
    """The final, date-specific rule values resolved by Ns Shift."""

    date: date
    required_day_staff: int
    required_night_staff: int
    required_leader_staff: int
    min_ability_level: int | None
    min_ability_level_staff_count: int | None
    max_consecutive_work_days: int
    night_shift_next_day_off: bool


class PreviousConsecutiveWorkInput(BaseModel):
    """A staff member's resolved consecutive-work count from the prior month."""

    staff_id: int
    previous_consecutive_work_days: int


class EffectiveOffDayInput(BaseModel):
    """The final monthly number of off days assigned to one staff member."""

    staff_id: int
    off_days: int


class AssignmentKeyInput(BaseModel):
    """Identifies a user-controlled assignment cell."""

    staff_id: int
    date: date


class GenerateShiftRequest(BaseModel):
    """The complete, JSON-serializable payload sent from Ns Shift."""

    month_dates: list[date]
    staff_members: list[StaffInput]
    fixed_assignments: list[FixedAssignmentInput]
    effective_rules: list[EffectiveRuleInput]
    previous_consecutive_work_days: list[PreviousConsecutiveWorkInput]
    effective_off_days: list[EffectiveOffDayInput]
    user_override_assignment_keys: list[AssignmentKeyInput]


class HealthResponse(BaseModel):
    status: str


class GenerateShiftResponse(BaseModel):
    status: str
    staff_count: int
    target_day_count: int
