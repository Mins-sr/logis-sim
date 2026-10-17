# 現在の作業

## 要求・受け入れ条件

実プログラムを`src/`に集約した状態を維持し、設定を`config/`へ集約する。ルートの追跡対象を`.codex`、`.gitignore`、`.dockerignore`、`AGENTS.md`、`config/`、`docs/`、`scripts/`、`src/`、`tests/`に限定し、業務処理・公開API・DBスキーマを変えずに起動・ビルド・テストを成立させる。

## 配置方針

- `app`・`shared`・`worker`・`optimizer`・`db`・画面ソース・Nginx実行設定を`src/`配下へ移動する。
- backend、frontend、docker、envの設定を`config/`に置き、ルートに複製しない。
- `.local/`を実行時・依存関係・ビルド・キャッシュ・データ・ログ・利用台帳・バックアップの領域とし、クリーンチェックアウトには存在しなくてよい。
- Pythonパッケージ名は維持し、読み込みパスを`src`へ変更する。生成物・依存関係は`src/`へ入れない。

## 進捗・検証

- `config/layout.json`と`docs/AGENTS.md`にルートの追跡・隠し管理エントリ、種別、設定配置を明記した。
- ルートのnpm/Python/Alembic/Compose/Docker/env設定を`config/backend`、`config/frontend`、`config/docker`、`config/env`へ移動した。生成物と依存関係は`.local/`へ移した。
- `scripts/dev.sh`にsetup、frontendのdev/build/preview、api、worker、optimizer、migrate、compose、usageを追加し、任意のcwdと空白を含むパスから動作させた。setup以外は暗黙に依存関係を導入しない。
- frontend workspaceは`.local/frontend`へ設定をコピーし、`source -> ../../src/frontend`のシンボリックリンクを作成する。Vite/TypeScriptのリンク解決と`.local/frontend/dist`出力を確認した。
- `scripts/migrate_layout.py`は完全な事前衝突検査、同一内容の冪等性、失敗時の原本保持を備え、`scripts/migrate_db.py`はホスト側Alembicだけを実行する。Compose APIの自動マイグレーションは除去した。
- harnessにlayout scopeと前後検証、設定変更時の全体振り分け、移行・layout・cwd・ignoreのテストを追加した。harness coverage 86.1%、backend coverage 52.8%（既知の警告）、frontend build PASS。

- cost-aware-devの複雑な変更としてplanner → worker → 関連テスト → reviewerを適用した。
- 移動前のソース・設定を`.local/harness/baseline-src-layout/`へ保存し、既存DBのハッシュを記録した。
- `src/`へ実プログラムを集約し、Python import名、公開API、DBスキーマ、業務Pythonの内容を維持した。旧配置の`app`・`shared`・`worker`・`optimizer`・`db`・`frontend`・`proxy`は残っていない。
- layout前後検証、backend、harness、frontend buildをPASS。backendカバレッジ52.8%（既知警告）、harnessカバレッジ86.1%、harnessテスト59件。
- 通常Composeと開発overlayの設定展開を確認し、ホストの`.local/frontend` workspaceからVite buildを成功させた。Compose APIは自動マイグレーションを行わず、事前のhost migrationを要求する。
- 一時SQLiteのAlembic upgradeとコンテナ内の`app`・`worker`・`optimizer`・`shared` import、Viteの起動・TSX変換・`/driver`・`/shipper`入口を確認した。
- 既存DB 3個のハッシュと業務Python 16ファイルの内容は移動前後で一致した。業務処理・公開API・DBスキーマは変更していない。
- reviewer（GPT-5.6 Sol／high）は、開発Composeのbase統合、ホストmigration接続、移行ロールバック、frontend設定のstale検出、tracked root検査、`.local`出力先の7件を指摘した。同じworkerで1回修正し、59件のハーネステストと`--changed`全体検証を再実行してPASSとした。指定reviewerロールのGPT-5.4起動失敗は中断として記録し、Sol／highの独立レビューで確認した。
- 既存Postgresをダンプして`.local/backups/`へ保存し、`tof-logis_pg_data`ボリュームを維持したままDBコンテナだけを新Compose定義で再作成した。bind mountは`.local/backups`、Postgresはloopback公開、APIのready応答とDB healthcheckを確認した。
- 最終検証ログは`.local/harness/logs/verify-5rapd0_7/results.json`、補助検証ログは`.local/harness/src-layout-validation/`に保存した。

## 既知の制約

- 開発Composeではbase構成にViteサービスと開発用Nginx設定を重ね、APIとHMRを同じproxy入口へ転送する。依存関係は明示的setup時だけnamed volumeへ投入する。
- アプリのカバレッジ80%未達、フロントエンド単体テスト未整備は従来からの課題。
- Gitは初期コミット`e4579ab`（`refactor: organize runtime code under src`）を作成済み。pushは行っていない。
- frontend単体テストとアプリ全体80%カバレッジは未整備。backendカバレッジ52.8%は既知の警告で、worker・optimizerの専用テスト追加は別作業。
- 開発ComposeのVite転送と`.local/harness`への検証ログ・利用台帳集約を実装した。
- 消費クレジットは親・worker・reviewerで別々に記録した（`src-layout-parent-20261017`、`src-layout-worker-20261017-luna`、`src-layout-review-20261017-sol`）。いずれも実測値は未取得で、未計測のまま保持する。10月の記録は未計測6件、実測合計0、残額は暫定値として3,000。
