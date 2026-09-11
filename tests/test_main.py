from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


VALID_PAYLOAD = {
    "month_dates": ["2026-09-01", "2026-09-02"],
    "staff_members": [
        {
            "id": 1,
            "role": "leader",
            "ability_level": 5,
            "can_night_shift": True,
            "regular_days_off": [0, 6],
        },
        {
            "id": 2,
            "role": "member",
            "ability_level": 3,
            "can_night_shift": False,
            "regular_days_off": [],
        },
    ],
    "fixed_assignments": [
        {"staff_id": 1, "date": "2026-09-01", "shift_type": "night"}
    ],
    "effective_rules": [
        {
            "date": "2026-09-01",
            "required_day_staff": 8,
            "required_night_staff": 3,
            "required_leader_staff": 1,
            "min_ability_level": 4,
            "min_ability_level_staff_count": 2,
            "max_consecutive_work_days": 5,
            "night_shift_next_day_off": True,
        },
        {
            "date": "2026-09-02",
            "required_day_staff": 8,
            "required_night_staff": 3,
            "required_leader_staff": 1,
            "min_ability_level": None,
            "min_ability_level_staff_count": None,
            "max_consecutive_work_days": 5,
            "night_shift_next_day_off": True,
        },
    ],
    "previous_consecutive_work_days": [
        {"staff_id": 1, "previous_consecutive_work_days": 4}
    ],
    "effective_off_days": [{"staff_id": 1, "off_days": 9}],
    "user_override_assignment_keys": [
        {"staff_id": 1, "date": "2026-09-01"}
    ],
}


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_generate_accepts_ns_shift_payload() -> None:
    response = client.post("/generate", json=VALID_PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {
        "status": "received",
        "staff_count": 2,
        "target_day_count": 2,
    }


def test_generate_rejects_invalid_payload() -> None:
    invalid_payload = {
        **VALID_PAYLOAD,
        "staff_members": [{**VALID_PAYLOAD["staff_members"][0], "ability_level": 6}],
    }

    response = client.post("/generate", json=invalid_payload)

    assert response.status_code == 422
