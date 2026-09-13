import secrets

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
    assert "effective_rules" in response.json()["detail"]


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
