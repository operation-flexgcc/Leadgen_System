# Starter prompt for the new Codex project

Use this prompt after extracting the handoff ZIP and opening the `PROJECT` folder as a new local Codex project.

---

You are taking over the FlexGCC Outreach lead-generation-to-meeting application.

Begin with orientation only. Do not change code, commit, push, deploy, alter GitHub settings, access AWS, or mutate production data during the first pass.

Source baseline:

- Private GitHub repository: `https://github.com/gopalakrishnanplus-creator/FlexGCC-Outreach`
- Default branch: `main`
- Production-aligned commit: `2f634ab53dbbffcaf1364f33b7c88fe5a2319109`
- Production-aligned tree: `8f6af4dfbde0dff4c3b79acb066caeef183f1be4`
- Production URL: `https://leadgen.flexgcc.com`
- Handoff snapshot date: 3 September 2026, Asia/Kolkata

The extracted package contains a production-aligned committed source snapshot plus uncommitted handover documentation. It deliberately excludes secrets, the production environment file, database content, media, static build output, virtual environments, caches, and Git credentials. A Git bundle is supplied separately for offline repository history.

Recommended execution settings for the orientation pass:

- Model: `gpt-5.6-sol`, or the current reliable agentic workhorse if that model is unavailable.
- Reasoning effort: `high`; use `xhigh` for deployment, migration, authentication, or permission changes.
- Environment: local project rooted at the extracted `PROJECT` directory.
- Network: use read-only GitHub and public production checks where available. Do not use production write operations.

Read these files completely before proposing work:

1. `docs/FlexGCC_Outreach_Project_Handoff.docx`
2. `docs/FlexGCC_Outreach_Technical_Operations_Guide.docx`
3. `docs/FlexGCC_Outreach_Product_Overview.docx`
4. `docs/FlexGCC_Outreach_User_Manual.docx`
5. `README.md`
6. `.github/workflows/ci.yml`
7. `.github/workflows/deploy-production.yml`
8. `.github/workflows/deploy-aws.yml`
9. `.github/workflows/import-target-firms.yml`
10. `config/settings.py`, `config/urls.py`, `outreach/models.py`, `outreach/forms.py`, `outreach/views.py`, `outreach/api.py`, `outreach/api_documentation.py`, `outreach/permissions.py`, `outreach/workflows.py`, `outreach/bulk_import.py`, and `outreach/tests/`.

Then perform these read-only or local-only checks:

1. Inspect `git status`, remotes, branches, worktrees, HEAD, `origin/main`, tree hashes, and divergence. Preserve all pre-existing changes.
2. Confirm whether GitHub `main` still points to the recorded baseline or document the newer state. Treat current GitHub and production evidence as authoritative when they differ from this dated package.
3. Create a fresh virtual environment outside the repository or in ignored `.venv/`, install `requirements.lock.txt`, and run:

   ```bash
   python manage.py makemigrations --check --dry-run
   python manage.py check
   python manage.py test --noinput
   bash -n deploy.sh deploy/aws/deploy.sh
   ```

   Keep `DATABASE_URL` unset for isolated SQLite validation unless a dedicated non-production PostgreSQL test database has been explicitly supplied.

4. Verify `https://leadgen.flexgcc.com/health/` read-only. Do not create, edit, claim, import, roll back, or otherwise change production records as a test.
5. Compare the active EC2 workflow in `deploy-production.yml` with the alternate ECR/Systems Manager workflow in `deploy-aws.yml`. Do not assume the alternate path is live.
6. List missing access or evidence for GitHub administration, EC2, AWS account/region, database hosting/backups, DNS/TLS, Google OAuth, monitoring, and production secret custody. Never print or copy secret values.

Return an orientation report with exactly these sections:

1. **Current source identity** — local HEAD, remote `main`, tree hashes, divergence, and dirty state.
2. **Application understanding** — architecture, roles, principal user flows, data model, APIs, and major validation rules.
3. **Local readiness** — dependency installation, migration check, system check, test count/result, and any warnings.
4. **GitHub and delivery** — repository visibility, workflows, last verified runs, deployment triggers, known secret/variable names, and governance gaps.
5. **Production boundary** — public health result, documented server paths/services, what was verified live, and what remains unverified.
6. **Risks and contradictions** — especially any drift from this handoff, duplicate deployment paths, missing access, or unsafe assumptions.
7. **Recommended next step** — one concrete action, with no implementation until I approve or provide a new task.

Operating rules for subsequent work:

- Production behavior and the current production-aligned `main` branch are the baseline unless an approved release changes them.
- Every push to `main` can trigger production deployment. Do not push or merge without explicit authorization.
- Preserve data and unrelated work. Use migrations for schema changes and assess rollback compatibility before release.
- Browser and API workflows must enforce the same role, ownership, validation, and audit rules.
- Never create production prospect, outreach, import, token, or user records merely to test.
- Keep runtime secrets outside the repository and handoff documents.
- Update the user, product, technical, and project-handoff documents whenever visible behavior, architecture, or operations change.

---

