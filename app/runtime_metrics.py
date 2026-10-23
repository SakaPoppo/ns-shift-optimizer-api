"""API内部のCPU・メモリと実行環境を計測する。"""

from __future__ import annotations

import os
import resource
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import ortools
from ortools.sat.python import cp_model


@dataclass(frozen=True)
class RuntimeMeasurement:
    wall_started_at: float
    cpu_started_at: float

    @classmethod
    def start(cls) -> RuntimeMeasurement:
        return cls(
            wall_started_at=time.perf_counter(),
            cpu_started_at=time.process_time(),
        )

    def finish(
        self,
        *,
        context_build_seconds: float,
        model_build_seconds: float | None,
        result_build_seconds: float,
    ) -> dict:
        internal_elapsed_seconds = time.perf_counter() - self.wall_started_at
        cpu_time_seconds = time.process_time() - self.cpu_started_at
        return {
            "internal_elapsed_seconds": internal_elapsed_seconds,
            "context_build_seconds": context_build_seconds,
            "model_build_seconds": model_build_seconds,
            "result_build_seconds": result_build_seconds,
            "cpu_time_seconds": cpu_time_seconds,
            "cpu_percent": (
                cpu_time_seconds / internal_elapsed_seconds * 100
                if internal_elapsed_seconds > 0
                else None
            ),
            "peak_memory_mb": _peak_memory_mb(),
            "logical_cpu_count": os.cpu_count(),
            "cpu_limit": configured_cpu_limit(),
            "ortools_version": ortools.__version__,
            "num_search_workers": configured_worker_count(),
            "solver_seed": configured_solver_seed(),
        }


def configured_worker_count() -> int:
    return int(os.environ.get("ORTOOLS_NUM_SEARCH_WORKERS", "8"))


def configured_solver_seed() -> int:
    configured = os.environ.get("ORTOOLS_RANDOM_SEED")
    if configured is not None:
        return int(configured)
    return int(cp_model.CpSolver().parameters.random_seed)


def configured_cpu_limit() -> float | None:
    configured = os.environ.get("OPTIMIZER_CPU_LIMIT")
    if configured:
        return float(configured)
    return _cgroup_cpu_limit()


def _cgroup_cpu_limit() -> float | None:
    cpu_max_path = Path("/sys/fs/cgroup/cpu.max")
    if cpu_max_path.exists():
        quota, period = cpu_max_path.read_text(encoding="utf-8").split()
        if quota != "max":
            return int(quota) / int(period)
    quota_path = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period_path = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota_path.exists() and period_path.exists():
        quota = int(quota_path.read_text(encoding="utf-8"))
        period = int(period_path.read_text(encoding="utf-8"))
        if quota > 0 and period > 0:
            return quota / period
    return None


def _peak_memory_mb() -> float:
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return peak_rss / divisor
