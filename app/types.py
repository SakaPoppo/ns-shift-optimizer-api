"""
最適化API内で共有する基本型・ドメイン型を定義する。
schemas.pyのHTTPモデルとは分離し、context.py以降で使うPythonの内部データ構造を表す。
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Annotated

from pydantic import Field


# API入力で検証する値の範囲。能力はLv1〜5、曜日は月曜0〜日曜6。
AbilityLevel = Annotated[int, Field(ge=1, le=5)]
Weekday = Annotated[int, Field(ge=0, le=6)]


@dataclass(frozen=True)
class OptimizerStaff:
    """Solverが必要とする、Djangoモデルに依存しないスタッフ情報。"""

    id: int
    role: str
    ability_level: int
    can_night_shift: bool
    regular_days_off: tuple[int, ...]


@dataclass(frozen=True)
class EffectiveRule:
    """Ns Shiftで優先ルールを解決した後の日別勤務条件。"""

    required_day_staff: int
    required_day_staff_override: int | None
    required_night_staff: int
    required_leader_staff: int
    min_ability_level: int | None
    min_ability_level_staff_count: int | None
    max_consecutive_work_days: int
    night_shift_next_day_off: bool


@dataclass(frozen=True)
class OptimizationContext:
    """OR-Toolsモデルを構築するための、Django非依存の入力データ。"""

    month_dates: list[date]
    staff_members: list[OptimizerStaff]
    fixed_assignments: dict[tuple[int, date], str]
    effective_rules: dict[date, EffectiveRule]
    previous_consecutive_work_days: dict[int, int]
    effective_off_days: dict[int, int]
    configured_off_days: dict[int, int]
    user_override_assignment_keys: set[tuple[int, date]] = field(
        default_factory=set
    )

    @property
    def max_consecutive_work_days(self) -> int:
        """日別ルールに共通で設定される月内の最大連勤数を返す。"""

        try:
            return self.effective_rules[self.month_dates[0]].max_consecutive_work_days
        except IndexError as error:
            raise OptimizationError("対象日がないため最適化できません。") from error


# --- 各最適化フェーズが作るCP-SAT変数・評価値の入れ物 ---


@dataclass
class DayStaffingBalanceData:
    actual_day_count_vars: dict[date, object] = field(default_factory=dict)
    required_day_counts: dict[date, int] = field(default_factory=dict)
    day_staffing_delta_vars: dict[date, object] = field(default_factory=dict)
    total_planned_day_cells: int = 0
    day_count: int = 0
    high_day_staffing_overrides: dict[date, int] = field(default_factory=dict)
    reserved_high_override_cells: int = 0
    normal_day_cells: int = 0
    normal_dates: tuple[date, ...] = ()
    normal_floor_target: int = 0
    normal_ceil_target: int = 0
    normal_extra_cells: int = 0
    high_override_deviation_vars: dict[date, object] = field(
        default_factory=dict
    )
    maximum_high_override_deviation: object | None = None
    total_high_override_deviation: object | None = None
    normal_day_count_range: object | None = None
    normal_target_band_deviation: object | None = None
    low_override_ceil_penalty: object | None = None
    minimum_actual_day_count: object | None = None
    maximum_actual_day_count: object | None = None
    actual_day_count_range: object | None = None
    total_actual_day_count: object | None = None
    objective_score: object | None = None


@dataclass
class NightCountBalanceData:
    night_count_min: object | None = None
    night_count_max: object | None = None
    night_count_vars: dict[int, object] = field(default_factory=dict)
    night_balance_violation: object | None = None
    objective_score: object | None = None


@dataclass
class DayAbilityBalanceData:
    """日勤のLv別構成比と能力合計を評価するCP-SAT変数群。"""

    staff_count: int = 0
    staff_level_counts: dict[int, int] = field(default_factory=dict)
    total_staff_ability: int = 0
    actual_day_count_vars: dict[date, object] = field(default_factory=dict)
    level_count_vars: dict[tuple[date, int], object] = field(
        default_factory=dict
    )
    level_deviation_vars: dict[tuple[date, int], object] = field(
        default_factory=dict
    )
    daily_ability_total_vars: dict[date, object] = field(
        default_factory=dict
    )
    ability_total_deviation_vars: dict[date, object] = field(
        default_factory=dict
    )
    max_level_deviation: object | None = None
    total_level_deviation: object | None = None
    max_ability_total_deviation: object | None = None
    total_ability_total_deviation: object | None = None
    objective_score: object | None = None


@dataclass
class NightAbilityBalanceData:
    """夜勤可能スタッフの平均能力を基準に夜勤能力合計を評価する。"""

    eligible_staff_count: int = 0
    eligible_staff_ability_total: int = 0
    required_night_counts: dict[date, int] = field(default_factory=dict)
    daily_ability_total_vars: dict[date, object] = field(
        default_factory=dict
    )
    deviation_vars: dict[date, object] = field(default_factory=dict)
    max_deviation: object | None = None
    total_deviation: object | None = None
    objective_score: object | None = None


@dataclass(frozen=True)
class OptimizationPhaseDefinition:
    name: str
    objective: object
    max_time_seconds: int


@dataclass(frozen=True)
class OptimizationPhaseResult:
    name: str
    status: str
    objective_value: int | None
    optimal: bool
    solver: object | None


@dataclass(frozen=True)
class ShiftOptimizationOutput:
    """回答生成に必要な、最終Solver状態と各フェーズの結果。"""

    solver: object
    solver_status: str
    shift_vars: dict
    day_staffing_balance_data: DayStaffingBalanceData
    night_count_balance_data: NightCountBalanceData
    long_streak_terms: list
    phase_results: list[OptimizationPhaseResult]


class OptimizationError(Exception):
    """入力矛盾またはSolverの正常終了失敗を表す基底例外。"""


class InfeasibleOptimizationError(OptimizationError):
    """入力は正しいが、両立する勤務配置が存在しないことを表す例外。"""
