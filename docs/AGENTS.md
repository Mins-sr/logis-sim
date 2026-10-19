# Layout rules

## Human-only document

`docs/readme.md` is reserved for the human project owner. Follow the root
`AGENTS.md` restrictions: do not read, edit, summarize, quote, index, attach,
copy, or transmit its contents, including through subagents and bulk tools.
Only content-free metadata checks are allowed. Do not recreate a missing file.
It is excluded from Git and Docker builds; filesystem mode `600` restricts
other OS users but cannot distinguish the owner from AI running as that owner.

The repository root has a deliberately small tracked surface:

```text
.codex/ .gitignore .dockerignore AGENTS.md config/ docs/ scripts/ src/ tests/
```

`.git` is Git's management directory and `.local/` is the ignored local
runtime area. Virtual environments, dependency directories, build output,
caches, backups, and editor metadata are also hidden management entries. The
complete allowlist is in [`config/layout.json`](../config/layout.json).
The allowed hidden names are `.git`, `.local`, `.venv`, `.venv312`,
`.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `.coverage`, `.coverage.*`,
`.DS_Store`, `.cache`, `node_modules`, `dist`, and `backups`.
Unknown root entries and file/directory kind mismatches are layout errors.

`config/` is the source of truth for configuration. Backend settings live in
`config/backend/`, frontend settings in `config/frontend/`, Compose and
Dockerfiles in `config/docker/`, and the safe environment template in
`config/env/.env.example`.
Local secrets belong in the ignored `config/env/.env`; Compose receives that
file when present and the host migration helper uses its `DATABASE_URL`.

All generated runtime, dependency, build, cache, data, log, and usage-ledger
files belong below `.local/`. The directory may be absent in a clean checkout.
Use `scripts/dev.sh setup` to create it; commands that need the workspace
report setup instructions instead of installing implicitly.

`scripts/migrate_layout.py` (or `scripts/migrate.sh layout`) performs the
collision-safe configuration move. `scripts/migrate_db.py` (or
`scripts/migrate.sh db`) performs host-side Alembic checks and applies.
