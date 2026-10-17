# 構成と変更箇所の案内

## 基本のファイル構成

```text
project/
├── .codex/                # agent and project configuration
├── .gitignore/.dockerignore
├── AGENTS.md
├── config/                # all backend/frontend/docker/env configuration
│   ├── backend/           # Python dependencies, pytest, Alembic
│   ├── frontend/          # npm manifest, Vite, TypeScript
│   ├── docker/            # Compose files and Dockerfiles
│   └── env/               # safe environment template
├── docs/                  # design, verification, and current state
├── scripts/               # development and verification entrypoints
├── src/                   # application and runtime source
└── tests/                 # application and harness tests
```

`config/` is the only configuration source. Root copies of the npm, Python,
Compose, Docker, and environment files are intentionally absent. Runtime,
dependency, build, cache, data, log, ledger, and backup output belongs under
the ignored `.local/` directory. The directory may be absent in a clean
checkout. The complete root allowlist is in
[`config/layout.json`](../config/layout.json) and [`docs/AGENTS.md`](AGENTS.md).

Python package names remain `app`, `shared`, `worker`, and `optimizer`; the
developer entrypoint sets `PYTHONPATH=src` without changing imports.

## 実装済みの構成

| 責務 | 実装 | 関連テスト |
| --- | --- | --- |
| 認証、参加、コマンド受付、状態照会、WebSocket | `src/app/main.py` | `tests/test_api.py` |
| DB接続・SQLAlchemyモデル | `src/app/db.py`、`src/app/models.py` | APIテスト |
| コマンド処理、セッション所有権、時計、最適化依頼 | `src/worker/main.py` | 専用テストは未整備 |
| 非同期の最適化受付・計算・結果保存 | `src/optimizer/main.py` | 専用テストは未整備 |
| 配送→集荷の順序、積載量、時間枠の計算 | `src/shared/vrpb.py` | `tests/test_vrpb.py` |
| AGV・ステージング予約とスナップショット | `src/shared/warehouse.py` | `tests/test_warehouse.py` |
| React画面、API呼び出し、通知表示 | `src/frontend/` | 型検査とビルド。単体テストなし |
| 検証・クレジット記録 | `scripts/` | `tests/harness/` |

ブラウザ → Nginx → API → DB内のコマンド → worker → optimizer、という流れ
です。通常起動は、事前に`scripts/dev.sh migrate apply`を実行してから
`scripts/dev.sh compose up -d --build`です。公開入口は既定で
`http://localhost:8080`、Composeのプロジェクト名は`tof-logis`です。
`pg_data`など既存の名前付きPostgresボリュームは維持します。

## 開発設定とマイグレーション

`scripts/dev.sh setup`だけが依存関係を導入します。他のコマンドは不足時に
セットアップ方法を表示して終了し、暗黙にインストールしません。frontendは
`.local/frontend`へ設定をコピーし、`source -> ../../src/frontend`の相対シンボリック
リンクを作成してViteを実行します。ViteとTypeScriptはリンクを解決し、
ビルド出力とキャッシュを`.local/frontend`内へ置きます。

`scripts/dev.sh migrate check`はホスト側のAlembicで未適用差分を確認し、
`migrate apply`は対象DBへ適用します。設定ファイル移行は
`scripts/migrate_layout.py check|apply`です。どちらも
`docker compose exec`を使わず、稼働中コンテナへマイグレーションを適用
しません。PostgreSQLを使う場合は`DATABASE_URL`を設定し、DBがhealthyに
なってからホスト側で実行します。Compose APIの起動コマンドにも自動
マイグレーションを含めません。`config/env/.env`があればComposeと
`scripts/dev.sh migrate`が読み込み、ComposeのPostgreSQLはホストの
`localhost:${POSTGRES_PORT:-5432}`へ公開されます。

## 設計と実装の区別

詳しい目標仕様は[設計書v1.2](SLAPStack_VRPB_設計書_v1.2.md)の必要な節
だけ参照してください。設計書の記述だけで実装済みと判断しません。

- 現在の倉庫処理は独自の予約・スナップショット用アダプタです。
- optimizerの地点間所要時間は決定論的な近似値です。
- VRPBは小規模な入力で列挙法を使い、OR-Tools経路も持ちます。
- 荷主・ドライバー・ホストは共通React画面を使います。

配置の整理では、これらの業務機能、公開API、DBスキーマを変更しません。
