# FlexGCC Outreach transfer and setup checklist

## 1. Package integrity

- [ ] Verify the ZIP SHA-256 supplied with the delivery.
- [ ] Extract the ZIP and verify `MANIFEST_SHA256.txt` from the package root.
- [ ] Confirm `PROJECT/` contains the application code, migrations, tests, templates, static source, deployment files, and four Word documents.
- [ ] Confirm no `.env`, database, media, virtualenv, cache, private key, or Git credential is present.

## 2. Source control

- [ ] Obtain access to the private repository `gopalakrishnanplus-creator/FlexGCC-Outreach`.
- [ ] Confirm `main`, remote HEAD, current commit, tree hash, and divergence before changing anything.
- [ ] Review the current GitHub branch/review policy. Do not assume branch protection is active.
- [ ] Confirm ownership of GitHub Actions secrets `EC2_IP` and `EC2_SSH` without revealing their values.
- [ ] Review Actions history and identify the current production workflow.
- [ ] Decide whether the packaged documentation should be committed and pushed as a separate approved documentation release.

## 3. Local development

- [ ] Use Python 3.13 where practical to match CI and the container image.
- [ ] Create a fresh virtual environment; do not copy one between machines.
- [ ] Install `requirements.lock.txt` and run `pip check`.
- [ ] Copy `.env.example` to `.env` only when local OAuth or PostgreSQL configuration is needed.
- [ ] Keep `DATABASE_URL` unset for isolated SQLite use, or point it only to a dedicated development database.
- [ ] Run migration, Django, test, deployment-script syntax, and static-asset checks.
- [ ] For local Google OAuth, configure `http://localhost:8000/accounts/google/login/callback/` exactly.

## 4. Production and AWS access

- [ ] Confirm AWS account ID and legal owner, active region, IAM administrators, MFA, and break-glass process.
- [ ] Confirm the EC2 instance, network path, security groups, disk capacity, patch status, and SSH/deploy-key lifecycle.
- [ ] Confirm the live checkout `/var/www/leadgen/repo`, virtualenv `/var/www/leadgen/venv`, protected environment `/var/www/leadgen/repo/.env`, systemd service `leadgen`, and socket `/run/leadgen/leadgen.sock`.
- [ ] Confirm the PostgreSQL host, version, database/user ownership, encryption, backup retention, point-in-time recovery, maintenance window, and latest restore test.
- [ ] Confirm Nginx configuration, DNS owner, registrar, TLS renewal method, and emergency contacts.
- [ ] Confirm Google OAuth project ownership, approved origins/redirect URIs, secret rotation date, and recovery contacts.
- [ ] Confirm monitoring, alert recipients, availability target, log retention, incident channel, and escalation tree.

## 5. Release safety

- [ ] Treat `.github/workflows/deploy-production.yml` as the current live path until independently disproved.
- [ ] Treat `.github/workflows/deploy-aws.yml` and `deploy/aws/` as an alternate path unless all required AWS variables and live ownership are verified.
- [ ] Require committed migrations and tests for data-model or workflow changes.
- [ ] Take and verify a backup before destructive or irreversible database changes.
- [ ] Verify deployment commit, `/health/`, systemd service, Gunicorn socket, Nginx, and affected read-only user journeys after release.
- [ ] Prefer a forward-fix or revert commit on `main`; do not leave production on a detached historical commit.

## 6. Handoff acceptance

- [ ] New owner can clone or restore repository history from the supplied bundle.
- [ ] Fresh local setup passes all checks and 102 baseline tests.
- [ ] New owner can read GitHub Actions and identify the currently deployed commit.
- [ ] New owner can perform a read-only production health and log review.
- [ ] Secret custody and recovery ownership are documented outside this package.
- [ ] Outstanding risks, backlog, access removals, and documentation gaps each have an owner and due date.

