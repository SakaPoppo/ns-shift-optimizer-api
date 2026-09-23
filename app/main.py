"""Ns Shift Optimizer APIのHTTP入口。

HTTP層の責務は認証・入力受信・JSONレスポンス化だけに限定する。
実際の最適化は context.py と optimization.py へ委譲し、DjangoやDBには接続しない。
"""

from fastapi import Depends, FastAPI, HTTPException

from .auth import require_optimizer_api_key
from .context import build_optimization_context
from .optimization import optimize_shift
from .results import (
    build_generate_shift_response,
    build_infeasible_generate_shift_response,
)
from .schemas import (
    GenerateShiftRequest,
    GenerateShiftResponse,
    HealthResponse,
)
from .types import InfeasibleOptimizationError, OptimizationError


app = FastAPI(
    title="Ns Shift Optimizer API",
    description="Cloud Run API boundary for Ns Shift shift generation.",
    version="0.1.0",
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """認証不要の稼働確認。DBやSolverを呼ばないため軽量に応答する。"""

    return HealthResponse(status="ok")


@app.post(
    "/generate",
    response_model=GenerateShiftResponse,
    dependencies=[Depends(require_optimizer_api_key)],
)
def generate_shift(payload: GenerateShiftRequest) -> GenerateShiftResponse:
    """認証済みの生成要求を処理し、JSON化済みの結果だけを返す。"""

    try:
        # 1. HTTP入力を内部コンテキストへ正規化・検証する。
        context = build_optimization_context(payload)
        # 2. OR-Toolsで制約を満たす勤務表を探索する。
        optimization = optimize_shift(context)
        # 3. Solver内部オブジェクトを含まないAPIレスポンスへ変換する。
        return build_generate_shift_response(
            context=context,
            optimization=optimization,
        )
    except InfeasibleOptimizationError:
        # 制約が両立しない場合も、画面表示に必要な構造化レスポンスを返す。
        return build_infeasible_generate_shift_response(context=context)
    except OptimizationError as error:
        # 入力整合性エラーなど、利用者が修正可能な問題は422に統一する。
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_OPTIMIZER_REQUEST"},
        ) from error
