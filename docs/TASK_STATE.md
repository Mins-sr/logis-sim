# 現在の作業

状態：スキル・指示書の更新完了。Sol／highの独立レビュー2件を同じLuna／xhighのworkerが
1回で修正し、親が該当箇所を確認した。構成検査の既知の不具合は未解決。今回の変更はコミット対象で、pushは未実施。

## 要求・受入条件

cost-aware-devのskillとプロジェクト運用文書を、実際に公開されているrole metadataと
利用記録CLIに合わせて更新する。skillのYAMLの`name`と`description`は維持し、skillは
汎用英語、プロジェクト文書は日本語とする。対象は次の4ファイルに限定し、source・script・
config・`.codex`・履歴文書は変更しない。

- `/Users/toma/.codex/skills/cost-aware-dev/SKILL.md`
- `AGENTS.md`
- `docs/TESTS.md`
- `docs/TASK_STATE.md`

## 実施内容

- skillから固定モデル、固定effort、role fileのsandbox保証を除き、公開metadataとの照合、
  不一致時の承認値付き汎用agent、利用不可時の未実施／中断、権限は実行ツールが決めることを明記した。
- simpleを明白で局所的な1実装領域、normalを複数の関連実装ファイル・layerまたは独立レビューが
  必要な変更として分類し、test-onlyの付随ファイルだけではnormalへ上げない基準を追加した。
- 親タスクの質問・承認調整・小規模文書編集・commit操作、リスク別routing、簡潔な引継ぎ、
  acceptance -> method -> result/evidence、独立レビュー、1回の修正・再検証を文書化した。
- docs変更の`--changed`は`layout` scopeになること、利用記録のrecord／settle／summary、
  未計測・重複ID・JST暦月・project/account usageの区別を`docs/TESTS.md`に反映した。
- roleの承認済みmodel／effortとruntime named role metadataを比較し、両方を扱えるgeneric agent
  がない場合は代替せず中断すること、失敗起動でmodel不明・ledger未対応の場合は観測事実と理由を
  `ledger-registration pending`として残すことを追加した。

## 検証・証跡

| 受入条件 | 方法 | 結果・証跡 |
| --- | --- | --- |
| skill frontmatterと構文 | `quick_validate.py`（`.local/venv/bin/python`、PyYAML 6.0.3） | **PASS／exit 0**。`/Users/toma/project/tof-logis/.local/harness/cost-aware-update-20261017T225926+0900/targeted-fix-quick-validate.txt` |
| 差分に空白エラーなし | `git diff --check` | **PASS／exit 0**。`/Users/toma/project/tof-logis/.local/harness/cost-aware-update-20261017T225926+0900/targeted-fix-diff-check.txt` |
| reviewer指摘と文書整合性 | routing、role metadata、failed launch fallback、scope/review wordingのfocused consistency check | **PASS／exit 0**。`/Users/toma/project/tof-logis/.local/harness/cost-aware-update-20261017T225926+0900/targeted-fix-consistency.txt` |
| layout契約 | `scripts/verify.sh --scope layout`を1回 | **PASS／exit 0**。`/Users/toma/project/tof-logis/.local/harness/cost-aware-update-20261017T225926+0900/layout-verify.txt`（詳細結果 `/Users/toma/project/tof-logis/.local/harness/logs/verify-2ynii7re/results.json`） |
| 独立レビューと指摘対応 | Sol／highの読み取りレビュー、同じworkerの1回修正、親の対象確認 | **2件修正済み・親が確認**。証跡フォルダ内の`independent-review.md`と`parent-targeted-confirmation.md`。全体の独立レビューは繰り返していない |

## 独立レビューと今回の修正

- routingのsimple／normal境界が曖昧で、関連する複数実装ファイルがreviewerを省く余地があったため、
  局所的な1実装領域と複数file／layerまたはreview必要性を明記した。
- 失敗起動のモデル不明・台帳未対応時の扱いが不足していたため、観測事実・理由を
  `ledger-registration pending`へ残す規則をskillとTESTSへ追加した。
- 親の確認で見つけた表現の誤りも同じ修正で直した。承認値とruntimeの同名roleを比較し、両方の
  承認値を扱えるgeneric agentだけを許可する。TESTSはscope自動選択と差分reviewを分離し、
  台帳の検証定義の参照先を`scripts/harness.py`へ修正した。親による該当箇所の確認は完了した。

証跡フォルダは`.local/harness/cost-aware-update-20261017T225926+0900/`。
修正後の構成検査は、対象の構造・設定・検証処理が不変のため上記の成功結果を再利用した。過去のアプリ検証で
確認済みのbackend coverage 52.8%とfrontend単体テスト未整備は既知の制約であり、この文書更新の
新規検証結果ではない。文書変更のため、アプリ全体build・test・auditは実施しない。

## 既知の制約と保留

- `config/layout.json`はrootの`node_modules`、`dist`、`backups`、legacy virtualenv/cache entry
  (`.venv`、`.venv312`、`.pytest_cache`、`.ruff_cache`、`.mypy_cache`、`.cache`)を許可しており、
  望ましいstrict layoutとは一致しない。この更新の対象外で、layout PASSはその不整合を修正した
  ことも証明したことも意味しない。

## 利用記録・次の一手

`.local/harness/usage.jsonl`へ次の3件を`success`・未計測で記録し、読み戻して確認した。
IDの共通部分は`cost-aware-update-20261017T225926JST-`。

| ID末尾 | モデル | 実施内容 |
| --- | --- | --- |
| `parent` | `gpt-6-astra` | 委譲、証跡・修正確認、記録 |
| `worker` | `gpt-5.6-luna` | 4文書の編集、1回の指摘修正と対象検証 |
| `reviewer` | `gpt-5.6-sol` | 独立レビュー完了、2件の修正指摘 |

モデルの起動失敗はなかった。最初の台帳登録はIDの`+`が入力検証で拒否され、書き込みなしで
終了したため、許可文字だけのIDに直して登録した。架空の起動失敗レコードは追加していない。
2026年10月は全9件が未計測で、実消費・残額は確定できない。集計の実測合計0は消費0を意味しない。

今回の範囲に保留事項はない。次の別作業は、旧ルート配置を許可する構成検査の修正と回帰テスト。
