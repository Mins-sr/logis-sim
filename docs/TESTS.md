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

`--changed`は未追跡・未ステージ・ステージ済みを合算し、自動検証のscopeだけを選択します。
`docs/`だけの変更は`layout` scopeへ振り分けられます。文書内容の差分確認は別の人手または
agentのreviewで行い、`--changed`自体が差分reviewを完了したとはみなしません。
`AGENTS.md`、ignore、config、entrypoint、migrationの変更はlayoutを含む全体検証へ
振り分けられます。対象パスが空のときだけ`SKIP`となるため、文書変更を一律に
`SKIP`とは記録しません。

この更新のように`AGENTS.md`を含む文書変更では、アプリケーション全体を再実行せず
`scripts/verify.sh --scope layout`を明示します。`--changed`は`AGENTS.md`を含む差分を
全scopeへ振り分けるためです。

## 受入条件・証跡・再利用

受入条件ごとに、`acceptance -> method -> result and evidence`の順で記録します。
検証コマンドの終了コード、対象パス、結果ファイルまたはログを証跡に残し、実行検証と
独立レビューを別欄で扱います。実施していない項目は`not-tested`として保留し、成功扱い
にしません。

過去の成功結果を再利用できるのは、対象となるソース・設定・依存関係・実行環境が変わらず、
結果ファイルまたはログの場所を確認できる場合だけです。それ以外は該当scopeを再実行します。
文書だけの変更では、全体のアプリケーションsuiteを広げず、差分確認と必要なlayout検証を
行います。

## 起動・マイグレーション

最初に使う入口は `scripts/start.sh` です。macOSでは `scripts/start.command` を
Finderからダブルクリックでき、LinuxやSSHでは次のコマンドを実行します。

```sh
scripts/start.sh
scripts/start.sh --no-open
```

この入口はDocker/Compose、Python、Node/npmを確認し、必要なら明示的な
`scripts/dev.sh setup`を実行した後、他プロジェクトのコンテナ停止、`tof-logis` ComposeのDB起動、ホスト側の
PostgreSQLマイグレーション、全サービスのbuild/up、`/health/ready`とトップページの
HTTP確認を順番に行います。起動URLは `http://127.0.0.1:8080/` として表示されます。
失敗した工程のログは `.local/logs/start/` に残ります。停止は
`scripts/dev.sh compose down`（名前付きDBボリュームは保持）です。
8080が使用中の場合は、例えば `PROXY_PORT=8081 scripts/start.sh --no-open` として
localhost内の別ポートを指定できます。

他コンテナ停止は、接続中のDocker環境に対して毎回、ポート競合に関係なく実施します。
`com.docker.compose.project`が正確に`tof-logis`と一致するものを除き、起動中の
全コンテナを対象とします。所属なしも対象、停止済みは対象外です。確認入力はありません。
コンテナIDを指定して通常停止（終了待機10秒）し、一覧取得・停止・再確認は各工程で
`--timeout`の期限を適用します。停止後に他コンテナが残存・再起動・新規起動していれば
DB起動前に失敗します。再確認後の起動を継続監視する機能はありません。
コンテナ・イメージ・ボリュームを削除せず、起動失敗時・終了時も他サービスを自動再開しません。
必要なサービスは利用者が再開します。Docker自体やホストの通常プロセスは停止しません。
対象一覧と結果は起動表示および`.local/logs/start/containers-{list,stop,verify}.log`に残ります。

この工程の回帰テストは`tests/harness/test_start.py`でDocker呼び出しを差し替え、
対象の判定、停止失敗・期限超過、再確認時の残存、DB起動順序を確認します。
実機では、MacのダブルクリックとLinuxの実行で、別プロジェクトが停止し、`tof-logis`と
データが保持されることを確認してください。実行前に他のDocker作業への影響を把握し、
必要なサービスは検証後にそのサービスの手順で再開します。

役割入口は、ログイン成功後に同じブラウザの別タブへ切替を通知し、別タブを入口へ戻します。
別タブでは、片方のログイン・役割切替が完了してから次の操作を行ってください。同時に異なる役割へ
ログインする操作や、BroadcastChannelに対応しないブラウザでの複数タブ利用は対象外です。

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

## 利用記録

利用記録のCLIは`scripts/dev.sh usage`です。説明用のIDや架空の消費量を実行せず、実在する
実行の観測値だけを記録します。以下の`<...>`は実際の値への置換例です。

```sh
scripts/dev.sh usage record --id <unique-run-id> --model <actual-observed-model> --status <success|failed|aborted> --source "<non-empty-source>"
scripts/dev.sh usage record --id <unique-run-id> --model <actual-observed-model> --status success --credits <measured-task-credits> --source "<non-empty-source>"
scripts/dev.sh usage settle --id <unique-unmeasured-id> --credits <measured-task-credits> --source "<non-empty-source>"
scripts/dev.sh usage summary --month <YYYY-MM>
```

`--credits`を省略したrecordは未計測として保持します。実測値を記録するrecordとsettleには
空でない`--source`が必要です。settleできるのは同じタスクの未計測レコードだけで、既に
実測値のあるレコードを上書きしません。親、worker、reviewer、起動失敗はそれぞれ異なる
IDを使い、重複IDは拒否されます。起動失敗は実行結果に応じて`aborted`または`failed`とし、
利用できない承認済みモデルを別モデルへ置き換えません。

`--model`は実際に観測したモデルを指定し、statusも実際の結果に合わせます。現在のledgerが
受け付けるモデルは`scripts/harness.py`の`MODELS`にある`gpt-5.6-luna`、`gpt-5.6-sol`、
`gpt-6-astra`、statusは`success`、`failed`、`aborted`です。月集計は日本時間（JST）の
暦月で行い、未計測を含む場合の残額は暫定値です。アカウント全体の利用量、token数、利用率は
このプロジェクトledgerのクレジットとして記録しません。

起動失敗でモデルを観測できない場合、または観測モデルがledgerで受け付けられない場合は、
親・worker・reviewer・失敗起動ごとの一意なID、観測できた事実、理由を`docs/TASK_STATE.md`へ
`ledger-registration pending`として残します。ledgerに登録できないモデルやクレジットを捏造せず、
別モデルへ置き換えません。受け付け可能な事実が得られた後に、親タスクが既存CLIで記録または
settleします。
