# Ns Shift Optimizer API

Ns Shift のシフト自動生成処理を、Django本体から分離して実行するためのFastAPIサービスです。

最終的にはGoogle Cloud Run上でOR-Toolsを実行し、Render上のNs ShiftからHTTP経由でシフト生成を依頼する構成を予定しています。

## Architecture

```text
Render
┌──────────────────────────┐
│ Ns Shift                 │
│ Django                   │
│                          │
│ DBから生成条件を取得        │
│ ↓                        │
│ API用JSONを作成           │
└────────────┬─────────────┘
             │
             │ HTTP / JSON
             ▼
Cloud Run
┌──────────────────────────┐
│ Ns Shift Optimizer API   │
│ FastAPI + OR-Tools       │
│                          │
│ JSONを受信                │
│ ↓                        │
│ シフト最適化               │
│ ↓                        │
│ 結果をJSONで返却           │
└────────────┬─────────────┘
             │
             ▼
         Ns Shift
             │
             ▼
        DjangoがDBへ保存
```

このAPIはDjangoやNs Shiftのデータベースへ直接接続しません。

スタッフ情報、勤務条件、固定勤務、前月からの連勤数など、シフト生成に必要な情報はNs Shift側でJSONへ変換して送信します。

## Current Status

現在は、API基盤とDjango非依存のOR-Tools最適化器まで実装済みです。

実装済み：

* DockerによるFastAPI実行環境
* `GET /health`
* `POST /generate`
* PydanticによるNs Shift payloadのvalidation
* `GenerateShiftRequest` から `OptimizationContext` への変換
* OR-Toolsによる内部シフト最適化
* APIテスト

未実装：

* 生成結果JSONの返却
* `/generate` から最適化器を呼び出す処理
* Ns Shiftからの実際のHTTP通信
* Cloud Runへのデプロイ

現在の `/generate` は受信したpayloadを検証し、スタッフ数と対象日数のみ返します。

## Project Structure

```text
.
├── app/
│   ├── __init__.py
│   ├── constants.py
│   ├── context.py
│   ├── main.py
│   ├── optimization.py
│   ├── schemas.py
│   └── types.py
├── tests/
│   ├── test_context_and_optimization.py
│   └── test_main.py
├── Dockerfile
├── requirements.txt
├── .dockerignore
├── .gitignore
└── README.md
```

### `app/main.py`

FastAPIのエントリーポイントです。

`/health` や `/generate` などのAPIエンドポイントを定義します。

### `app/schemas.py`

Ns Shiftから送信されるJSONの構造をPydanticモデルとして定義します。

### `app/types.py`

能力レベルや曜日など、複数のschemaから利用する共通型を定義します。

### `tests/`

APIの正常系・validation errorなどをテストします。

## Run with Docker

Docker imageをビルドします。

```bash
docker build -t ns-shift-optimizer-api .
```

コンテナを起動します。

```bash
docker run --rm -p 8080:8080 ns-shift-optimizer-api
```

起動後、以下へアクセスできます。

```text
Health Check
http://localhost:8080/health

Swagger UI
http://localhost:8080/docs
```

`/health` が以下を返せばAPIは正常に起動しています。

```json
{
  "status": "ok"
}
```

## POST /generate

Ns Shiftのシフト生成用payloadを受け取るエンドポイントです。

現在のHTTP endpointはpayloadのvalidationのみを行います。OR-Tools最適化器はAPI内部で利用でき、次の実装でHTTP endpointから呼び出して結果JSONへ変換します。

正常なpayloadを送信すると、例えば以下を返します。

```json
{
  "status": "received",
  "staff_count": 20,
  "target_day_count": 30
}
```

## Run Tests

Docker imageをビルドします。

```bash
docker build -t ns-shift-optimizer-api .
```

ローカルの `tests` ディレクトリをコンテナへマウントしてテストします。

```bash
docker run --rm \
  -v "$(pwd)/tests:/app/tests:ro" \
  ns-shift-optimizer-api \
  python -m pytest -p no:cacheprovider tests
```

## Next Step

次の実装では、最適化結果をHTTPレスポンス用JSONへ変換し、`/generate` から最適化器を呼び出します。

最終的には、

```text
Ns Shift
↓
POST /generate
↓
Cloud Run
↓
OR-Tools
↓
生成結果JSON
↓
Ns Shift
↓
DB保存
```

までを実現します。
