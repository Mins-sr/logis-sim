# 現在の作業

## 要求・受け入れ条件

実プログラムを`src/`に集約し、開発環境・ビルド設定を分離する。`AGENTS.md`・`docs/`・`scripts/`・`tests/`を維持し、業務処理・公開API・DBスキーマを変えずに起動・ビルド・テストを成立させる。

## 配置方針

- `app`・`shared`・`worker`・`optimizer`・`db`・画面ソース・Nginx実行設定を`src/`配下へ移動する。
- npmのmanifest・lockfile・Vite/TypeScript設定はルート、Dockerfileは`docker/`。Compose・Python開発設定はルートに維持する。
- Pythonパッケージ名は維持し、読み込みパスを`src`へ変更する。生成物・依存関係は`src/`へ入れない。

## 進捗・検証

- cost-aware-devの複雑な変更としてplanner → worker → 関連テスト → reviewerを適用した。
- 移動前のソース・設定を`.harness/baseline-src-layout/`へ保存し、既存DBのハッシュを記録した。
- `src/`へ実プログラムを集約し、Python import名、公開API、DBスキーマ、業務Pythonの内容を維持した。旧配置の`app`・`shared`・`worker`・`optimizer`・`db`・`frontend`・`proxy`は残っていない。
- 全9工程PASS。backendテスト6件、harnessテスト44件、backendカバレッジ52.8%（既存警告）、harnessカバレッジ95.1%、frontend build成功。
- 通常Composeと開発overlayの設定展開、API・proxyのDocker build、開発frontendコンテナの`npm ci && npm run build`を確認した。
- 一時SQLiteのAlembic upgradeとコンテナ内の`app`・`worker`・`optimizer`・`shared` import、Viteの起動・TSX変換・`/driver`・`/shipper`入口を確認した。
- 既存DB 3個のハッシュと業務Python 16ファイルの内容は移動前後で一致した。業務処理・公開API・DBスキーマは変更していない。
- reviewer（GPT-5.6 Sol／high）を1回実行し、パス漏れ・開発設定混入・ビルド起動影響について指摘なし。修正と再検証は不要だった。
- 最終検証ログは`.harness/logs/verify-272xxx8r/results.json`、補助検証ログは`.harness/src-layout-validation/`に保存した。

## 既知の制約

- 元の開発Composeの`DEV_MODE`引数は未実装で、Nginxは静的成果物を配信する。今回Vite転送の機能追加はしない。
- アプリのカバレッジ80%未達、フロントエンド単体テスト未整備は従来からの課題。
- Gitは初期コミット`e4579ab`（`refactor: organize runtime code under src`）を作成済み。pushは行っていない。
- frontend単体テストとアプリ全体80%カバレッジは未整備。backendカバレッジ52.8%は既知の警告で、worker・optimizerの専用テスト追加は別作業。
- 元の開発Composeの`DEV_MODE`引数は未実装で、Nginxは静的成果物を配信する。今回Viteへのproxy転送は追加していない。
- 消費クレジットは親・worker・reviewerで別々に記録した（`src-layout-parent-20261017`、`src-layout-worker-20261017-luna`、`src-layout-review-20261017-sol`）。いずれも実測値は未取得で、未計測のまま保持する。10月の記録は未計測6件、実測合計0、残額は暫定値として3,000。
