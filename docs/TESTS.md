# 検証と省コスト運用

## 初回準備

macOS/Linux、Bash、Git、Python 3.12、Node.js 20を使用します。Python補助スクリプトは標準ライブラリだけで動作し、検証時には固定した開発依存が必要です。

```sh
python3.12 -m venv .venv312
.venv312/bin/python -m pip install -r requirements-dev.txt
npm ci
```

`verify.sh`は`HARNESS_PYTHON`、`.venv312/bin/python`、`.venv/bin/python`、`python3`の順で選択します。Pythonパスを明示する場合は絶対パスを使ってください。検証中の依存インストールや自動修正はありません。フロントエンドはルートの既存lockfileから導入します。`npm run dev`、`npm run build`、`npm run preview`もルートで実行します。画面のソースは`src/frontend/`、ビルド出力はルートの`dist/`です。

## 最小の検証から始める

```sh
# DBを使わないルーティングの関連テストだけ
.venv312/bin/python -m pytest tests/test_vrpb.py -q
# 対象ごとの検証
scripts/verify.sh --scope backend
scripts/verify.sh --scope frontend
scripts/verify.sh --scope harness
# 作業中の差分から選択／統合前の全体検証
scripts/verify.sh --changed
scripts/verify.sh
```

APIテストはルートで直接実行せず、`--scope backend`を使用します。既存の`test_api.py`はDBを作り直すため、ハーネスが一時作業ディレクトリから起動してローカルDBを保護します。

| 対象 | チェック |
| --- | --- |
| backend | 構文確認、Ruffの致命的エラー（E9/F63/F7/F82）、pytest、分岐を含むカバレッジ |
| frontend | `npm run build`によるTypeScript検査とViteビルド。単体テスト未整備を警告 |
| harness | Ruff、Bash構文、pytest、分岐を含むカバレッジ80%以上 |

backendの測定対象は`src/app`・`src/shared`・`src/worker`・`src/optimizer`です。テストコードを含めず、未実行モジュールも分母に含めます。現段階ではアプリの80%未達は警告、テスト失敗はエラーです。ハーネスの80%未達はエラーです。Dockerの起動確認、ブラウザ試験、依存脆弱性監査はこのスクリプトのPASSに含みません。

`--changed`は未追跡・未ステージ・ステージ済みを合算し、削除・名前変更も対象にします。初回コミット前は全体検証です。共通設定や分類不能な変更も全体検証、文書だけなら`SKIP`です。既にコミット済みのブランチ差分は対象にしないため、統合前は引数なしで検証してください。Git未初期化での`--changed`はエラーです。

## 起動・マイグレーションと新しい配置

ローカルでAPIを個別に起動する場合は`PYTHONPATH=src .venv312/bin/python -m uvicorn app.main:app --reload`を使用します。worker・optimizerも同じ`PYTHONPATH=src`で従来のモジュール名を指定します。pytestにはルートの設定で`src`と`.`を渡し、ハーネスは絶対パスでこれらを設定します。

Alembicの設定はルートに残し、`src/db/migrations/`を参照します。既存DBを変更せずにパスを検証するには、一時ディレクトリのSQLiteを明示します。

```sh
migration_check_dir=$(mktemp -d)
DATABASE_URL="sqlite:///$migration_check_dir/check.db" .venv312/bin/python -m alembic upgrade head
```

Dockerfileは`docker/`、Nginxの実行設定は`src/proxy/nginx.conf`です。Composeの通常・開発設定はルートに残ります。開発用frontendサービスは必要な画面ソースとルートのビルド設定だけをマウントし、Linux用の依存関係を`frontend_node_modules`ボリュームへ保存します。

既存の開発ComposeにはViteサービスがありますが、Nginxは静的なビルド成果物を配信します。`DEV_MODE`引数による切り替えは未実装で、この配置変更でViteへの転送機能は追加していません。通常のブラウザ確認はビルド済みCompose、画面単体の開発はローカルの`npm run dev`を使います。

## 出力と失敗時の対応

出力は最大40行です。`PASS`は選択した検証の成功、`FAIL`は失敗、`WARN`は既知の不足、`SKIP`はコード検証なしを表します。全体の終了コードは成功0、検証失敗1、引数・設定・記録エラー2です。工程別の終了コードは詳細にも保存し、起動失敗は127、10分のタイムアウトは124、中断は130です。

詳細は`.harness/logs/verify-*/`の工程別ログ、JUnit XML、カバレッジJSON、`results.json`に保存します。成功したログ全文をモデルに読ませません。失敗した工程の要約を起点に、必要な位置だけ読みます。

```sh
rg -n 'FAILED|Error|ERROR' .harness/logs/verify-対象/backend-tests.log
```

同じ原因の修正・検証が2回失敗したら停止し、原因、試したこと、不足情報を`TASK_STATE.md`に残します。ログはローカルだけに保存されるため、不要になった分は手動で整理してください。

## モデルとタスクの開始

親タスクの既定はLuna／medium、サブエージェントは有効です。新規タスクで適用します。アプリのモデル選択やCLIの明示指定は設定より優先されるため、開始時に表示を確認します。プロジェクト設定は信頼済みプロジェクトで読み込まれます。グローバル設定は変更しません。

```sh
# 調査・軽微な修正：1要求・1起動
codex -m gpt-5.6-luna -c model_reasoning_effort='"medium"' --enable multi_agent
# 複雑な実装：自動昇格させず開始時に選択
codex -m gpt-5.6-sol -c model_reasoning_effort='"medium"' --enable multi_agent
```

実装・修正・レビューでは`$cost-aware-dev <変更要求と受け入れ条件>`を使用します。単純な変更はworkerだけ、通常の変更はworkerとreviewer、複雑な変更はplannerを追加し、高リスクのレビューはdeep_reviewerに置き換えます。役割は`.codex/agents/`で定義します。planner・reviewer系は読み取り専用です。利用不可のモデルは勝手に置き換えません。開始時には変更要求・受け入れ条件・対象パスを渡し、設計書全文や過去ログを添付しません。ローカル検証・予算集計はCodexを起動しません。

| 役割 | この環境のモデル／推論強度 | 権限 |
| --- | --- | --- |
| worker | `gpt-5.6-luna`／xhigh | workspace-write |
| planner | `gpt-6-astra`／medium | read-only |
| reviewer | `gpt-5.6-sol`／high | read-only |
| deep_reviewer | `gpt-6-astra`／medium | read-only |

原本のworkerはGPT-6 Luna、reviewerはGPT-6.1 Solですが、導入時のこの環境の利用可能一覧にはありません。既存のLuna／Solのモデル方針に合わせ、上表の利用可能な5.6系を明示的に割り当てています。原本のスキルは改変せず、プロジェクトの役割設定で適用します。実行時にモデルを自動変更しません。

サブエージェントは同時に最大2体、委譲は1段までです。工程は基本的に順次実行し、レビュー中だけ同じworkerを残して修正へ引き継げます。編集担当と読み取り専用の担当を分け、引き継ぎでは要点・対象パス・受け入れ条件だけ渡します。具体的な新規不具合への修正・対象箇所の再検証は原則1回までです。ユーザー側の旧planner/reviewer設定より、このプロジェクトの役割定義を優先します。既に開始済みのタスクでは役割一覧が更新されない場合があるため、設定後の新規タスクで使用してください。

スキルは[ROBthePRO/skillsのcost-aware-dev](https://github.com/ROBthePRO/skills/tree/main/cost-aware-dev)を使用し、実体は`~/.codex/skills/cost-aware-dev/SKILL.md`です。他の環境では`skill-installer`に`cost-aware-dev/codex`を`cost-aware-dev`の名前で導入させてください。インストール後は次のターンから利用できます。

## クレジット記録

[公式の使用量案内](https://learn.chatgpt.com/docs/pricing)に従い利用画面を確認します。CLIの`/status`は利用枠の確認に使えますが、残り割合を当該タスクのクレジットへ換算しません。プロジェクトの実消費を区別できた場合だけ実測として記録します。他のタスクと重なる場合は未計測です。

```sh
# 未計測として記録（モデルは実際に使ったものを指定）
.venv312/bin/python scripts/harness.py usage record --id task-001-luna-1 --model gpt-5.6-luna --status success
# 実測値が後から分かった場合だけ精算
.venv312/bin/python scripts/harness.py usage settle --id task-001-luna-1 --credits 12.5 --source '利用画面・当該タスクのみ'
# 最初から実測値がある場合
.venv312/bin/python scripts/harness.py usage record --id task-002-sol-1 --model gpt-5.6-sol --status failed --credits 15 --source '利用画面・当該タスクのみ'
# 今月、または指定月を集計
.venv312/bin/python scripts/harness.py usage summary
.venv312/bin/python scripts/harness.py usage summary --month 2026-10
```

IDは親タスク・サブエージェントそれぞれの1回のモデル実行ごとに一意にします。モデル切替・再実行時は別IDを使い、結果は`success`・`failed`・`aborted`から選びます。`--at`でタイムゾーン付きISO日時を指定でき、省略時は記録時刻です。精算しても元の月は変わりません。実測値には測定元が必須で、負数・不正値・重複IDは拒否します。

JSTの暦月で集計し、実測合計2,400以上で警告、3,000以上で新規作業の停止を促します。未計測は0にせず件数を表示し、残額を暫定扱いにします。会計上の強制上限や自動起動ゲートはありません。トークン推計を実請求として扱わず、アカウント全体の消費をこのプロジェクトへ丸ごと計上しません。

記録は`.harness/usage.jsonl`に保存します。Git対象外なので別途バックアップしてください。既に実測を記録した値の自動上書きは行いません。誤記を修正する場合はバックアップを取り、原資料と照合して手動で修正します。

## 開発者の画面確認とコミット

1. 必要に応じて`.env.example`を`.env`へコピーし、ローカル用設定を確認する。
2. `docker compose up -d --build`で起動し、`http://localhost:8080`を開く。
3. ローカルデモ資格情報でログインし、状態・案件一覧・倉庫リソースが表示されることを確認する。
4. ホスト権限で速度変更と計画生成を試し、状態と通知の更新、ブラウザの通信エラーを確認する。
5. `/shipper`と`/driver`の表示を確認する。これは設計書の全機能の試験ではない。
6. `cost-aware-dev`のリスク分類に応じて差分レビューし、結果・未解決事項を`TASK_STATE.md`に記録する。開発者の確認後に必要なコミットを行う。

初回コミット前は全ファイルが未追跡なので`git diff`だけでは内容を確認できません。`git status --short`で対象一覧を確認し、コミットする段階で意図したファイルだけステージして`git diff --cached`を確認します。秘密情報、ローカルDB、バックアップ、生成物、ログはコミットしません。

モデル設定の参照：[公式設定リファレンス](https://learn.chatgpt.com/docs/config-file/config-reference)。
