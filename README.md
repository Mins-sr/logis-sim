# tof-logis

単一デポの配送市場シミュレーションです。荷主、ドライバー／運送事業者、管理者／ホスト／倉庫運営者の3役で、案件登録から受注、計画生成、出発、配送、帰庫、倉庫資源の解放までを検証できます。

## 役割

- **荷主**：配送案件を登録し、案件の状態を確認します。
- **ドライバー／運送事業者**：車両を選び、案件を受注して計画を生成し、出発します。
- **管理者／ホスト／倉庫運営者**：実験環境、時計、速度、一時停止、倉庫資源を管理します。HOSTは1セッション1名です。

## 構成

- `src/app/`：FastAPI API、認証、状態取得、コマンド受付
- `src/worker/`：コマンド処理、時計、運行、通知outbox
- `src/optimizer/`：車両別の配送計画生成
- `src/shared/`：VRPBと倉庫資源の共通モデル
- `src/frontend/`：役割別の操作画面
- `src/db/migrations/`：Alembicマイグレーション
- `tests/`：API、worker、optimizer、倉庫、起動ハーネスのテスト

## 開発

依存関係の導入と開発用サービスの操作は、プロジェクト標準スクリプトを使用します。

```sh
scripts/dev.sh setup
scripts/dev.sh api
scripts/dev.sh worker
scripts/dev.sh optimizer
scripts/verify.sh --scope backend
scripts/verify.sh --scope frontend
```

Docker Composeを使う場合は、Composeプロジェクト名 `tof-logis` と既存のPostgreSQLボリュームを維持してください。マイグレーションはサービス内で直接実行せず、`scripts/dev.sh migrate apply` を使います。

## ライセンス

このプロジェクトは [GNU General Public License v3.0 or later](https://www.gnu.org/licenses/gpl-3.0.html) の下で公開します。詳細は [LICENSE](LICENSE) を参照してください。
