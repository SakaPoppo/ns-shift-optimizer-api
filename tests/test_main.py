import secrets
from datetime import date
from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient

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
from app.main import app
from app.optimization import SUCCESSFUL_OPTIMIZATION_STATUSES
from app.results import _build_generation_issues


client = TestClient(app)
API_KEY = secrets.token_urlsafe(32)


def api_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


@pytest.fixture(autouse=True)
def configured_optimizer_api_key(monkeypatch):
    monkeypatch.setenv("OPTIMIZER_API_KEY", API_KEY)


def make_payload(
    *,
    days: int = 3,
    staff_members: list[dict] | None = None,
    required_night_staff: list[int] | None = None,
    off_days: dict[int, int] | None = None,
    configured_off_days: dict[int, int] | None = None,
    fixed_assignments: list[dict] | None = None,
    previous_consecutive_work_days: list[dict] | None = None,
    user_override_assignment_keys: list[dict] | None = None,
    max_consecutive_work_days: int = 5,
) -> dict:
    dates = [f"2026-09-{day:02d}" for day in range(1, days + 1)]
    staff_members = staff_members or [
        {
            "id": 1,
            "role": "leader",
            "ability_level": 5,
            "can_night_shift": True,
            "regular_days_off": [],
        }
    ]
    required_night_staff = required_night_staff or [0] * days
    off_days = off_days or {staff["id"]: 0 for staff in staff_members}
    configured_off_days = configured_off_days or off_days
    return {
        "month_dates": dates,
        "staff_members": staff_members,
        "fixed_assignments": fixed_assignments or [],
        "effective_rules": [
            {
                "date": target_date,
                "required_day_staff": 0,
                "required_night_staff": required_night_staff[index],
                "required_leader_staff": 0,
                "min_ability_level": None,
                "min_ability_level_staff_count": None,
                "max_consecutive_work_days": max_consecutive_work_days,
                "night_shift_next_day_off": True,
            }
            for index, target_date in enumerate(dates)
        ],
        "previous_consecutive_work_days": previous_consecutive_work_days or [],
        "effective_off_days": [
            {"staff_id": staff_id, "off_days": count}
            for staff_id, count in off_days.items()
        ],
        "configured_off_days": [
            {"staff_id": staff_id, "off_days": count}
            for staff_id, count in configured_off_days.items()
        ],
        "user_override_assignment_keys": user_override_assignment_keys or [],
    }


def shift_type_for(response_json: dict, *, staff_id: int, target_date: str) -> str:
    return next(
        shift["shift_type"]
        for shift in response_json["shifts"]
        if shift["staff_id"] == staff_id and shift["date"] == target_date
    )


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_generate_returns_serializable_optimization_result() -> None:
    response = client.post("/generate", json=make_payload(), headers=api_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["solver_status"] in SUCCESSFUL_OPTIMIZATION_STATUSES
    assert len(body["shifts"]) == 3
    assert {phase["name"] for phase in body["phase_results"]} == {
        "night_count_balance",
        "night_ability_balance",
        "day_staffing_balance",
        "day_ability_balance",
        "long_streak",
    }
    assert all("solver" not in phase for phase in body["phase_results"])
    assert body["issues"][0] == {
        "code": "SHIFT_GENERATED",
        "severity": "success",
        "dates": [],
        "staff_ids": [],
        "details": {},
    }
    assert {issue["code"] for issue in body["issues"]} == {
        "SHIFT_GENERATED",
        "DAY_STAFFING_ABOVE_REQUIRED",
    }
    assert not {
        issue["code"]
        for issue in body["issues"]
        if issue["severity"] in {"warning", "error"}
    }


def test_generation_issues_are_json_safe_and_match_django_criteria() -> None:
    first_date = date(2026, 9, 1)
    second_date = date(2026, 9, 2)
    optimization = SimpleNamespace(
        solver=SimpleNamespace(Value=lambda expression: expression),
        day_staffing_balance_data=SimpleNamespace(
            actual_day_count_vars={first_date: 3, second_date: 5},
            required_day_counts={first_date: 5, second_date: 5},
            day_staffing_delta_vars={first_date: -2, second_date: 0},
            minimum_delta=-2,
            maximum_delta=0,
        ),
        night_count_balance_data=SimpleNamespace(
            night_count_vars={12: 3, 18: 1},
        ),
        phase_results=[
            SimpleNamespace(name="day_ability_balance", status="UNKNOWN"),
            SimpleNamespace(name="night_ability_balance", status="NOT_RUN"),
            SimpleNamespace(name="long_streak", status="NOT_RUN"),
        ],
    )

    issues = _build_generation_issues(
        optimization=optimization,
    )
    serialized_issues = [issue.model_dump(mode="json") for issue in issues]
    issues_by_code = {issue["code"]: issue for issue in serialized_issues}

    assert issues_by_code["DAY_STAFFING_BELOW_REQUIRED"] == {
        "code": "DAY_STAFFING_BELOW_REQUIRED",
        "severity": "warning",
        "dates": ["2026-09-01"],
        "staff_ids": [],
        "details": {
            "actual_day_counts": {"2026-09-01": 3, "2026-09-02": 5},
            "required_day_counts": {"2026-09-01": 5, "2026-09-02": 5},
        },
    }
    assert issues_by_code["DAY_STAFFING_IMBALANCE"] == {
        "code": "DAY_STAFFING_IMBALANCE",
        "severity": "warning",
        "dates": ["2026-09-02"],
        "staff_ids": [],
        "details": {
            "actual_day_counts": {"2026-09-01": 3, "2026-09-02": 5},
            "required_day_counts": {"2026-09-01": 5, "2026-09-02": 5},
            "modal_day_staffing_count": 3,
            "count_difference_threshold": 2,
        },
    }
    assert issues_by_code["NIGHT_COUNT_IMBALANCE"] == {
        "code": "NIGHT_COUNT_IMBALANCE",
        "severity": "warning",
        "dates": [],
        "staff_ids": [12, 18],
        "details": {
            "minimum_count": 1,
            "maximum_count": 3,
            "count_difference": 2,
        },
    }
    assert issues_by_code["OPTIMIZATION_INCOMPLETE"]["details"] == {
        "incomplete_items": [
            "day_ability_balance",
            "night_ability_balance",
            "long_streak",
        ]
    }


def test_generate_preserves_fixed_night_and_its_after_night_constraint() -> None:
    response = client.post(
        "/generate",
        json=make_payload(
            required_night_staff=[1, 0, 0],
            off_days={1: 1},
            fixed_assignments=[
                {"staff_id": 1, "date": "2026-09-01", "shift_type": SHIFT_NIGHT}
            ],
        ),
        headers=api_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert shift_type_for(body, staff_id=1, target_date="2026-09-01") == SHIFT_NIGHT
    assert (
        shift_type_for(body, staff_id=1, target_date="2026-09-02")
        == SHIFT_AFTER_NIGHT
    )
    assert shift_type_for(body, staff_id=1, target_date="2026-09-03") == SHIFT_OFF


def test_generate_never_assigns_night_to_night_ineligible_staff() -> None:
    response = client.post(
        "/generate",
        json=make_payload(
            days=1,
            staff_members=[
                {
                    "id": 1,
                    "role": "leader",
                    "ability_level": 5,
                    "can_night_shift": False,
                    "regular_days_off": [],
                },
                {
                    "id": 2,
                    "role": "member",
                    "ability_level": 3,
                    "can_night_shift": True,
                    "regular_days_off": [],
                },
            ],
            required_night_staff=[1],
        ),
        headers=api_headers(),
    )

    assert response.status_code == 200
    assert (
        shift_type_for(response.json(), staff_id=1, target_date="2026-09-01")
        != SHIFT_NIGHT
    )


def test_generate_keeps_fixed_non_generated_shifts_in_response() -> None:
    response = client.post(
        "/generate",
        json=make_payload(
            days=4,
            off_days={1: 1},
            fixed_assignments=[
                {"staff_id": 1, "date": "2026-09-01", "shift_type": SHIFT_TRAINING},
                {"staff_id": 1, "date": "2026-09-02", "shift_type": SHIFT_PAID_LEAVE},
                {"staff_id": 1, "date": "2026-09-03", "shift_type": SHIFT_SPECIAL_LEAVE},
                {"staff_id": 1, "date": "2026-09-04", "shift_type": SHIFT_OFF_REQUEST},
            ],
        ),
        headers=api_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert [shift["shift_type"] for shift in body["shifts"]] == [
        SHIFT_TRAINING,
        SHIFT_PAID_LEAVE,
        SHIFT_SPECIAL_LEAVE,
        SHIFT_OFF_REQUEST,
    ]


def test_generate_applies_previous_work_streak_to_first_day() -> None:
    response = client.post(
        "/generate",
        json=make_payload(
            days=1,
            off_days={1: 1},
            previous_consecutive_work_days=[
                {"staff_id": 1, "previous_consecutive_work_days": 5}
            ],
        ),
        headers=api_headers(),
    )

    assert response.status_code == 200
    assert shift_type_for(response.json(), staff_id=1, target_date="2026-09-01") == SHIFT_OFF


def test_generate_preserves_user_override_then_forces_generated_off() -> None:
    override_keys = [
        {"staff_id": 1, "date": f"2026-09-0{day}"} for day in (1, 2, 3)
    ]
    response = client.post(
        "/generate",
        json=make_payload(
            days=4,
            off_days={1: 1},
            max_consecutive_work_days=2,
            fixed_assignments=[
                {"staff_id": 1, "date": item["date"], "shift_type": SHIFT_DAY}
                for item in override_keys
            ],
            user_override_assignment_keys=override_keys,
        ),
        headers=api_headers(),
    )

    assert response.status_code == 200
    assert shift_type_for(response.json(), staff_id=1, target_date="2026-09-04") == SHIFT_OFF


def test_generate_returns_meaningful_http_error_for_inconsistent_payload() -> None:
    payload = make_payload(days=2)
    payload["effective_rules"] = payload["effective_rules"][:1]

    response = client.post("/generate", json=payload, headers=api_headers())

    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "INVALID_OPTIMIZER_REQUEST"}}


def test_generate_returns_infeasible_result_for_unsatisfiable_conditions() -> None:
    response = client.post(
        "/generate",
        json=make_payload(
            days=1,
            required_night_staff=[1],
            off_days={1: 1},
        ),
        headers=api_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "infeasible",
        "solver_status": "INFEASIBLE",
        "shifts": [],
        "phase_results": [],
        "issues": [
            {
                "code": "GENERATION_INFEASIBLE",
                "severity": "error",
                "dates": [],
                "staff_ids": [],
                "details": {},
            }
        ],
    }


def test_generate_reports_leader_shortage_before_generic_fallback() -> None:
    staff_members = [
        {
            "id": 1,
            "role": "leader",
            "ability_level": 3,
            "can_night_shift": True,
            "regular_days_off": [],
        },
        {
            "id": 2,
            "role": "member",
            "ability_level": 3,
            "can_night_shift": True,
            "regular_days_off": [],
        },
    ]
    response = client.post(
        "/generate",
        json=make_payload(
            days=1,
            staff_members=staff_members,
            off_days={1: 1, 2: 0},
            fixed_assignments=[
                {"staff_id": 1, "date": "2026-09-01", "shift_type": SHIFT_OFF}
            ],
        )
        | {
            "effective_rules": [
                {
                    "date": "2026-09-01",
                    "required_day_staff": 1,
                    "required_night_staff": 0,
                    "required_leader_staff": 1,
                    "min_ability_level": None,
                    "min_ability_level_staff_count": None,
                    "max_consecutive_work_days": 5,
                    "night_shift_next_day_off": True,
                }
            ]
        },
        headers=api_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "infeasible"
    assert body["issues"] == [
        {
            "code": "INSUFFICIENT_LEADER_STAFF",
            "severity": "error",
            "dates": ["2026-09-01"],
            "staff_ids": [],
            "details": {"available_count": 0, "required_count": 1},
        },
    ]


def test_generate_reports_night_shortage_before_generic_fallback() -> None:
    response = client.post(
        "/generate",
        json=make_payload(
            days=1,
            staff_members=[
                {
                    "id": 1,
                    "role": "leader",
                    "ability_level": 3,
                    "can_night_shift": False,
                    "regular_days_off": [],
                }
            ],
            required_night_staff=[1],
        ),
        headers=api_headers(),
    )

    assert response.status_code == 200
    assert response.json()["issues"] == [
        {
            "code": "INSUFFICIENT_NIGHT_STAFF",
            "severity": "error",
            "dates": ["2026-09-01"],
            "staff_ids": [],
            "details": {"available_count": 0, "required_count": 1},
        }
    ]


def test_generate_allows_day_staffing_shortage_when_required_nights_are_possible() -> None:
    staff_members = [
        {
            "id": staff_id,
            "role": "leader" if staff_id == 1 else "member",
            "ability_level": 3,
            "can_night_shift": True,
            "regular_days_off": [],
        }
        for staff_id in range(1, 9)
    ]
    response = client.post(
        "/generate",
        json=make_payload(
            days=1,
            staff_members=staff_members,
            fixed_assignments=[
                {"staff_id": 1, "date": "2026-09-01", "shift_type": SHIFT_OFF},
                {"staff_id": 2, "date": "2026-09-01", "shift_type": SHIFT_OFF},
            ],
        )
        | {
            "effective_rules": [
                {
                    "date": "2026-09-01",
                        "required_day_staff": 5,
                    "required_night_staff": 2,
                    "required_leader_staff": 0,
                    "min_ability_level": None,
                    "min_ability_level_staff_count": None,
                    "max_consecutive_work_days": 5,
                    "night_shift_next_day_off": True,
                }
            ]
        },
        headers=api_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert sum(
        shift["shift_type"] == SHIFT_DAY for shift in body["shifts"]
    ) == 4
    assert sum(
        shift["shift_type"] == SHIFT_NIGHT for shift in body["shifts"]
    ) == 2
    issues_by_code = {issue["code"]: issue for issue in body["issues"]}
    assert "STAFFING_CAPACITY_SHORTAGE" not in issues_by_code
    assert issues_by_code["DAY_STAFFING_BELOW_REQUIRED"]["dates"] == [
        "2026-09-01"
    ]


def test_generate_warns_when_fixed_monthly_off_count_exceeds_configuration() -> None:
    staff_member = {
        "id": 1,
        "role": "leader",
        "ability_level": 3,
        "can_night_shift": True,
        "regular_days_off": [],
    }
    response = client.post(
        "/generate",
        json=make_payload(
            days=12,
            staff_members=[staff_member],
            off_days={1: 10},
            configured_off_days={1: 10},
            fixed_assignments=[
                {
                    "staff_id": 1,
                    "date": f"2026-09-{day:02d}",
                    "shift_type": SHIFT_OFF,
                }
                for day in range(1, 13)
            ],
        ),
        headers=api_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert {
        "code": "MONTHLY_OFF_COUNT_EXCEEDED",
        "severity": "warning",
        "dates": [],
        "staff_ids": [1],
        "details": {
            "configured_off_count": 10,
            "actual_off_count": 12,
            "excess_count": 2,
        },
    } in body["issues"]


def test_generate_rejects_invalid_payload() -> None:
    invalid_payload = make_payload()
    invalid_payload["staff_members"][0]["ability_level"] = 6

    response = client.post(
        "/generate", json=invalid_payload, headers=api_headers()
    )

    assert response.status_code == 422


def test_generate_accepts_correct_api_key() -> None:
    response = client.post("/generate", json=make_payload(), headers=api_headers())

    assert response.status_code == 200


def test_generate_rejects_missing_api_key() -> None:
    response = client.post("/generate", json=make_payload())

    assert response.status_code == 401


def test_generate_rejects_invalid_api_key() -> None:
    response = client.post(
        "/generate",
        json=make_payload(),
        headers={"X-API-Key": secrets.token_urlsafe(32)},
    )

    assert response.status_code == 401


def test_generate_is_unavailable_when_server_key_is_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("OPTIMIZER_API_KEY")

    response = client.post("/generate", json=make_payload())

    assert response.status_code == 503
