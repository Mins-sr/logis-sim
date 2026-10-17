# 構成と変更箇所の案内

## 基本のファイル構成

```text
project/
├── AGENTS.md
├── docs/                  # 設計、検証手順、現在の状態
├── scripts/               # verify.sh、検証・予算集計の補助処理
├── src/                   # 実プログラムと実行時の設定
│   ├── app/               # API・DBモデル
│   ├── shared/            # 共通の業務処理
│   ├── worker/            # シミュレーション処理
│   ├── optimizer/         # 最適化サービス
│   ├── frontend/          # 画面、CSS、HTMLの入口
│   ├── db/migrations/     # DBマイグレーション
│   └── proxy/             # Nginxの実行設定
├── tests/                 # アプリとハーネスのテスト
└── docker/                # イメージ構築用のDockerfile
```

開発・ビルドの設定（`package.json`、lockfile、Vite/TypeScript設定、`pyproject.toml`、`requirements*.txt`、`alembic.ini`、Compose設定）と`.codex/`はルートに置きます。`node_modules/`、仮想環境、`dist/`、`.harness/`はローカル生成物で、`src/`やGitに含めません。npmコマンドはルートで実行します。

Pythonのパッケージ名は`app`・`shared`・`worker`・`optimizer`を維持し、`src/`を読み込みパスに設定します。`src.app`のようなimportには変更しません。

## 実装済みの構成

このプロジェクトは物流シミュレーションの初期実装です。まず以下の表で対象を決め、`rg -n 'シンボル' 対象ディレクトリ`で必要な箇所だけ調べます。

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

ブラウザ → Nginx → API → DB内のコマンド → worker → optimizer、という流れです。状態とイベントはDBに保存し、APIが状態照会とWebSocket配信を担当します。通常起動は`docker compose up -d --build`で、公開入口は既定で`http://localhost:8080`です。API起動時にAlembicのマイグレーションを実行します。

Pythonコンテナは3.12、DBはPostgreSQL 16、フロントエンドビルドはNode.js 20です。ローカルAPIテストはSQLiteを使い、ハーネス経由では一時ディレクトリ内にDBを作ります。`src/db/migrations/versions/`にスキーマ変更を追加します。

## 設計と実装の区別

詳しい目標仕様は[設計書v1.2](SLAPStack_VRPB_設計書_v1.2.md)の必要な節だけ参照してください。設計書の記述だけで実装済みと判断しません。

- 現在の倉庫処理は独自の予約・スナップショット用アダプタです。実際のSLAPStackエンジン統合は依存関係・実装にありません。
- optimizerの地点間所要時間は決定論的な近似値です。道路行列の本格的な取込とは区別します。
- VRPBは小規模な入力で列挙法を使い、OR-Tools経路も持ちます。
- 荷主・ドライバー・ホストは共通React画面を使います。設計書の全操作画面が実装済みではありません。

配置の整理では、これらの業務機能、公開API、DBスキーマを変更しません。
