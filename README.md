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

現在は、Django非依存のOR-Tools最適化器をHTTP APIから実行し、生成結果をJSONで返却できます。

実装済み：

* DockerによるFastAPI実行環境
* `GET /health`
* `POST /generate`
* PydanticによるNs Shift payloadのvalidation
* `GenerateShiftRequest` から `OptimizationContext` への変換
* OR-Toolsによる内部シフト最適化
* `/generate` からOR-Tools最適化を実行
* 生成結果・最適化フェーズ結果のJSON返却
* 入力payloadのフィールド間整合性検証
* APIテスト

未実装：

* Ns Shiftからの実際のHTTP通信
* Cloud Runへのデプロイ

`/generate` はpayloadを検証した後にOR-Toolsを実行し、全スタッフ・全対象日について1件ずつ勤務結果を返します。固定の希望休・有給・特別休・研修もレスポンスに含まれます。

## Project Structure

```text
.
├── app/
│   ├── __init__.py
│   ├── constants.py
│   ├── context.py
│   ├── main.py
│   ├── optimization.py
│   ├── results.py
│   ├── schemas.py
│   └── types.py
├── tests/
│   ├── test_context_and_optimization.py
│   └── test_main.py
├── Dockerfile
├── requirements.txt
├── service.yaml
├── .dockerignore
├── .gitignore
└── README.md
```

### `app/main.py`

FastAPIのエントリーポイントです。

`/health` や `/generate` などのAPIエンドポイントを定義します。

### `app/schemas.py`

Ns Shiftから受信するJSONと、APIが返すJSONのPydanticモデルを定義します。

### `app/results.py`

OR-Toolsのsolver・変数・tuple keyを含む内部結果を、JSON安全なレスポンスモデルへ変換します。

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

Ns Shiftのシフト生成用payloadを受け取り、最適化済みのシフトを返すエンドポイントです。

正常なpayloadを送信すると、例えば以下を返します。

```json
{
  "status": "success",
  "solver_status": "OPTIMAL",
  "shifts": [
    {
      "staff_id": 1,
      "date": "2026-09-01",
      "shift_type": "day"
    }
  ],
  "phase_results": [
    {
      "name": "night_count_balance",
      "status": "OPTIMAL",
      "objective_value": 0,
      "optimal": true
    }
  ]
}
```

入力内のスタッフID・対象日・日別ルール・休日数が整合しない場合や、固定条件が成立しない場合は、HTTP 422と日本語の説明を返します。

## Cloud Run Configuration

Cloud Run用の設定は [service.yaml](service.yaml) でGit管理します。デプロイ前に、`IMAGE_URL` を実際のコンテナイメージURLへ置き換えてください。Project ID、リージョン、Artifact Registry、IAM、公開可否、Service Accountは、このファイルでは固定しておらず、実デプロイ時に決定します。

| 設定 | 値 | 目的 |
| --- | --- | --- |
| CPU | 2 vCPU | OR-Toolsの最適化を2 vCPUで実行するため。 |
| Memory | 2 GiB | 最適化モデルとsolverが必要とするメモリを確保するため。 |
| Concurrency | 1 | 同一インスタンスで重い生成処理を複数同時実行しないため。 |
| Min instances | 0 | 未使用時にscale-to-zeroしてコストを抑えるため。 |
| Max instances | 2 | 最大2件まで並列生成可能にしつつ、想定外のスケールアウトを抑えるため。 |
| Timeout | 300秒 | シフト生成に最大300秒を許可するため。 |
| Startup CPU Boost | ON | scale-to-zero後のコールドスタート時間を短縮するため。 |
| OR-Tools workers | 2 | 割り当てCPU数とOR-Tools worker数を揃えるため。 |
| Billing | request-based | リクエスト処理中を中心にCPUを割り当てるため。 |

DockerfileはCloud Runの`PORT`環境変数を優先し、未設定時は`8080`で`0.0.0.0`に待ち受けます。

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

Cloud Runへデプロイし、Ns ShiftからHTTPで生成依頼を送れるようにすると、

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
