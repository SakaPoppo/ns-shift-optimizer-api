"""保護対象の最適化APIに共通で使用するAPIキー認証。"""

from __future__ import annotations

import os
import secrets
from typing import Annotated

from fastapi import Header, HTTPException, status


def require_optimizer_api_key(
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    """設定済みの共有キーと一致するリクエストだけを許可する。

    サーバー側のキーが未設定の場合は、意図的に ``/generate`` を利用不可にする。
    ``/health`` にはこの依存関係を設定せず、Cloud Runの認証なしヘルスチェックを
    継続できるようにする。
    """

    configured_key = os.getenv("OPTIMIZER_API_KEY")
    if not configured_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Optimizer service authentication is not configured.",
        )

    if not x_api_key or not secrets.compare_digest(x_api_key, configured_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "API-Key"},
        )
