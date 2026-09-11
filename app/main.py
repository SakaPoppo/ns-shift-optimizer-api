"""FastAPI entry point for the Ns Shift optimizer service."""

from fastapi import FastAPI

from .schemas import (
    GenerateShiftRequest,
    GenerateShiftResponse,
    HealthResponse,
)


app = FastAPI(
    title="Ns Shift Optimizer API",
    description="Cloud Run API boundary for Ns Shift shift generation.",
    version="0.1.0",
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Return service health without contacting a database."""

    return HealthResponse(status="ok")


@app.post("/generate", response_model=GenerateShiftResponse)
def generate_shift(payload: GenerateShiftRequest) -> GenerateShiftResponse:
    """Validate an Ns Shift payload until OR-Tools generation is introduced."""

    return GenerateShiftResponse(
        status="received",
        staff_count=len(payload.staff_members),
        target_day_count=len(payload.month_dates),
    )
