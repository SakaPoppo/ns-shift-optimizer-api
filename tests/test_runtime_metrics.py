from __future__ import annotations

from app.runtime_metrics import (
    RuntimeMeasurement,
    configured_cpu_limit,
    configured_solver_seed,
    configured_worker_count,
)


def test_runtime_settings_come_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("OPTIMIZER_CPU_LIMIT", "1.5")
    monkeypatch.setenv("ORTOOLS_NUM_SEARCH_WORKERS", "3")
    monkeypatch.setenv("ORTOOLS_RANDOM_SEED", "11")

    assert configured_cpu_limit() == 1.5
    assert configured_worker_count() == 3
    assert configured_solver_seed() == 11


def test_runtime_measurement_returns_resource_metrics(monkeypatch) -> None:
    monkeypatch.setenv("OPTIMIZER_CPU_LIMIT", "2")
    monkeypatch.setenv("ORTOOLS_NUM_SEARCH_WORKERS", "2")
    monkeypatch.setenv("ORTOOLS_RANDOM_SEED", "1")
    measurement = RuntimeMeasurement.start()

    metrics = measurement.finish(
        context_build_seconds=0.01,
        model_build_seconds=0.02,
        result_build_seconds=0.03,
    )

    assert metrics["internal_elapsed_seconds"] >= 0
    assert metrics["cpu_time_seconds"] >= 0
    assert metrics["cpu_percent"] >= 0
    assert metrics["peak_memory_mb"] > 0
    assert metrics["cpu_limit"] == 2
    assert metrics["num_search_workers"] == 2
    assert metrics["solver_seed"] == 1
