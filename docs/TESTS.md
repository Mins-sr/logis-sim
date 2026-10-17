# 検証と省コスト運用

## 初回準備

macOS/Linux、Bash、Git、Python 3.12、Node.js 20を使用します。明示的な
セットアップだけが依存関係を導入します。

```sh
scripts/dev.sh setup
```

`verify.sh`は`HARNESS_PYTHON`、`.local/venv/bin/python`、既存の
`.venv312/bin/python`、`.venv/bin/python`、`python3`の順で選択します。
frontendの開発workspaceは`.local/frontend`で、画面ソースは
`src/frontend/`、ビルド出力は`.local/frontend/dist/`です。

## 最小の検証から始める

```sh
PYTHONPATH=src:. .local/venv/bin/python -m pytest tests/test_vrpb.py -q
scripts/verify.sh --scope layout
scripts/verify.sh --scope backend
scripts/verify.sh --scope frontend
scripts/verify.sh --scope harness
scripts/verify.sh --changed
scripts/verify.sh
```

| 対象 | チェック |
| --- | --- |
| layout | `config/layout.json`による許可エントリ、種別、生成物のignore確認（前後2回） |
| backend | 構文確認、Ruffの致命的エラー、pytest、分岐カバレッジ |
| frontend | `scripts/dev.sh frontend build`による型検査とViteビルド |
| harness | Ruff、Bash構文、pytest、分岐カバレッジ80%以上 |

backendの80%未達は既知の警告、ハーネスの80%未達はエラーです。Dockerの
起動確認、ブラウザ試験、依存脆弱性監査はPASSに含みません。

`--changed`は未追跡・未ステージ・ステージ済みを合算し、config、entrypoint、
layout、ignoreの変更はlayoutを含む全体検証へ振り分けます。文書だけなら
`SKIP`です。

## 起動・マイグレーション

```sh
scripts/dev.sh api
scripts/dev.sh worker
scripts/dev.sh optimizer
scripts/dev.sh frontend dev
scripts/dev.sh frontend build
scripts/dev.sh frontend preview
scripts/dev.sh migrate check
scripts/dev.sh migrate apply
scripts/dev.sh compose up -d --build
scripts/dev.sh compose --dev up
```

Compose reads `config/env/.env` when that file exists; copy the safe
`config/env/.env.example` there and edit it for local credentials. The `--dev`
overlay is applied on top of the base Compose file, serves the Vite workspace
through the proxy, and uses a named `frontend_node_modules` volume populated
by the explicit `scripts/dev.sh setup` command. It never installs dependencies
while starting services.

全コマンドは任意のカレントディレクトリと空白を含むパスから実行できます。
Pythonの既定SQLite、データ、ログは`.local/`配下です。Alembic設定は
`config/backend/alembic.ini`で、マイグレーションは`src/db/migrations/`を
参照します。`migrate apply`はホスト側で実行し、稼働中コンテナへ`exec`して
適用しません。

一時SQLiteで確認する例です。

```sh
migration_check_dir=$(mktemp -d)
DATABASE_URL="sqlite:///$migration_check_dir/check.db" scripts/dev.sh migrate apply
```

Postgresのデータは既存の`pg_data`名前付きボリューム、バックアップは
`.local/backups`に保持します。

## 出力と失敗時の対応

出力は最大40行です。`PASS`は成功、`FAIL`は検証失敗、`WARN`は既知の不足、
`SKIP`はコード検証なしを表します。工程別ログ、JUnit XML、カバレッジJSON、
`results.json`は`.local/harness/logs/verify-*/`に保存されます。10分の工程タイム
アウトは124、中断は130です。

```sh
rg -n 'FAILED|Error|ERROR' .local/harness/logs/verify-対象/*.log
```

同じ原因の修正・検証が2回失敗したら停止し、原因と不足情報を
`docs/TASK_STATE.md`へ残します。
