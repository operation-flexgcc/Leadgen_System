# FlexGCC Outreach handoff package

Package date: 3 September 2026, Asia/Kolkata

This archive is a portable transfer package for a new Codex project and the incoming engineering/operations owner.

## Package layout

```text
FlexGCC-Outreach-Handoff-2026-09-03/
├── PROJECT/                     Production-aligned source snapshot plus handoff documentation
├── GIT/
│   └── FlexGCC-Outreach.git.bundle  Offline Git repository history and refs
├── EVIDENCE/                    Dated source, GitHub, production-health, and packaging evidence
├── MANIFEST_SHA256.txt          SHA-256 for every packaged file except the manifest itself
└── PACKAGE_README.md            This file
```

Start a new Codex project at `PROJECT/`, then paste `PROJECT/handover/STARTER_PROMPT.md` as its first task.

## Source identity

- Repository: `https://github.com/gopalakrishnanplus-creator/FlexGCC-Outreach`
- Repository visibility: private
- Default branch: `main`
- Baseline commit: `2f634ab53dbbffcaf1364f33b7c88fe5a2319109`
- Baseline tree: `8f6af4dfbde0dff4c3b79acb066caeef183f1be4`
- Production: `https://leadgen.flexgcc.com`

The code snapshot is created from GitHub-aligned `origin/main`. The `PROJECT` folder then adds the current handover documentation and README links, which were intentionally not committed or pushed when this package was created.

## Restore or connect Git history

Preferred when GitHub access is available:

```bash
git clone https://github.com/gopalakrishnanplus-creator/FlexGCC-Outreach.git
```

Offline recovery from the supplied bundle:

```bash
git clone GIT/FlexGCC-Outreach.git.bundle FlexGCC-Outreach-restored
cd FlexGCC-Outreach-restored
git switch main
```

Copy the handover documentation from `PROJECT/docs/` and `PROJECT/handover/` into the restored checkout only if the receiving owner wants those uncommitted files in that working tree.

## Deliberate exclusions

The archive excludes:

- `.git/` working metadata and local credentials; full history is supplied as a portable Git bundle instead.
- `.env` and any production or local secret values.
- Private keys, API tokens, cookies, GitHub credentials, OAuth secrets, and database credentials.
- Local or production databases, database dumps, media, generated `staticfiles`, caches, logs, temporary files, and virtual environments.
- AWS account inventory, backup exports, or server snapshots that were not present in the repository.

Recreate environments from `requirements.lock.txt`, `.env.example`, `.env.production.example`, and the deployment documentation. Obtain real credentials through the approved secret-custody process, never through this ZIP.

## First verification

From `PROJECT/`:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.lock.txt
python manage.py makemigrations --check --dry-run
python manage.py check
python manage.py test --noinput
bash -n deploy.sh deploy/aws/deploy.sh
```

Use Python 3.13 to match GitHub CI and the application container when it is available. Keep `DATABASE_URL` unset for an isolated SQLite smoke test unless a dedicated non-production PostgreSQL database is explicitly supplied.

## Integrity verification

From the extracted package root:

```bash
shasum -a 256 -c MANIFEST_SHA256.txt
git bundle verify GIT/FlexGCC-Outreach.git.bundle
```

Do not run production writes merely to prove the package works. Public `/health/`, GitHub metadata, workflow history, and local tests provide the initial verification boundary.

