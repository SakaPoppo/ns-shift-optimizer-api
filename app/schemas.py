"""Ns ShiftとOptimizer API間で送受信するJSON形式を定義する。

Pydanticが型・必須項目・数値範囲を入口で検証し、context.py以降へ不正な入力を渡さない。
"""

from datetime import date
from pydantic import BaseModel, Field
from .types import AbilityLevel, Weekday


# --- Ns Shiftから受け取る生成条件 ---


class StaffInput(BaseModel):

    id: int
    role: str
    ability_level: AbilityLevel
    can_night_shift: bool
    regular_days_off: list[Weekday]


class FixedAssignmentInput(BaseModel):

    staff_id: int
    date: date
    shift_type: str


class EffectiveRuleInput(BaseModel):

    date: date
    required_day_staff: int
    required_day_staff_override: int | None = None
    required_night_staff: int
    required_leader_staff: int
    min_ability_level: int | None
    min_ability_level_staff_count: int | None
    max_consecutive_work_days: int
    night_shift_next_day_off: bool


class PreviousConsecutiveWorkInput(BaseModel):
    """前月末から引き継ぐ、スタッフごとの連勤数。"""

    staff_id: int
    previous_consecutive_work_days: int


class EffectiveOffDayInput(BaseModel):
    """固定勤務を反映後に、そのスタッフへ割り当てる月休日数。"""

    staff_id: int
    off_days: int


class ConfiguredOffDayInput(BaseModel):
    """設定画面で指定された、スタッフごとの月休日数。"""

    staff_id: int
    off_days: int


class AssignmentKeyInput(BaseModel):
    """ユーザーが手入力で確定した勤務セルを特定するキー。"""

    staff_id: int
    date: date


class GenerateShiftRequest(BaseModel):
    """Ns Shiftから送る、シフト生成に必要な全入力。"""

    month_dates: list[date]
    staff_members: list[StaffInput]
    fixed_assignments: list[FixedAssignmentInput]
    effective_rules: list[EffectiveRuleInput]
    previous_consecutive_work_days: list[PreviousConsecutiveWorkInput]
    effective_off_days: list[EffectiveOffDayInput]
    configured_off_days: list[ConfiguredOffDayInput] = Field(default_factory=list)
    user_override_assignment_keys: list[AssignmentKeyInput]


# --- Optimizer APIから返す生成結果 ---


class HealthResponse(BaseModel):
    status: str


class GeneratedShiftOutput(BaseModel):
    """スタッフ1人・対象日1日の確定勤務。"""

    staff_id: int
    date: date
    shift_type: str


class OptimizationPhaseOutput(BaseModel):
    """最適化フェーズ1つ分の、JSONで返せる実行結果。"""

    name: str
    status: str
    objective_value: int | None
    optimal: bool


class GenerationIssueResponse(BaseModel):
    """Djangoが表示・マーキングに使う、機械可読な生成結果の事実。"""

    code: str
    severity: str
    dates: list[date] = Field(default_factory=list)
    staff_ids: list[int] = Field(default_factory=list)
    details: dict[str, object] = Field(default_factory=dict)


class GenerateShiftResponse(BaseModel):
    status: str
    solver_status: str
    shifts: list[GeneratedShiftOutput]
    phase_results: list[OptimizationPhaseOutput]
    issues: list[GenerationIssueResponse]
