"""FastAPI entry point for the Ns Shift optimizer service."""

from fastapi import Depends, FastAPI, HTTPException

from .auth import require_optimizer_api_key
from .context import build_optimization_context
from .optimization import optimize_shift
from .results import build_generate_shift_response
from .schemas import (
    GenerateShiftRequest,
    GenerateShiftResponse,
    HealthResponse,
)
from .types import OptimizationError


app = FastAPI(
    title="Ns Shift Optimizer API",
    description="Cloud Run API boundary for Ns Shift shift generation.",
    version="0.1.0",
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Return service health without contacting a database."""

    return HealthResponse(status="ok")


@app.post(
    "/generate",
    response_model=GenerateShiftResponse,
    dependencies=[Depends(require_optimizer_api_key)],
)
def generate_shift(payload: GenerateShiftRequest) -> GenerateShiftResponse:
    """Generate a shift plan and return only JSON-serializable result data."""

    try:
        context = build_optimization_context(payload)
        optimization = optimize_shift(context)
        return build_generate_shift_response(
            context=context,
            optimization=optimization,
        )
    except OptimizationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
