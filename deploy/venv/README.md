# deploy on EC2 — production operations

The only active GitHub Actions workflow is [deploy-on-ec2.yml](../../.github/workflows/deploy-on-ec2.yml), named **deploy on EC2**. It runs automatically on pushes to `main`, or manually using **Actions → deploy on EC2 → Run workflow → main**. Other refs cannot deploy. The previous CI, EC2, AWS/SSM, and target-firm-import workflow files were removed; their history remains in Git.

## Existing production layout

| Setting | Value |
|---|---|
| Repository | `operation-flexgcc/Leadgen_System` |
| Release branch | `main` |
| SSH user / port | `ubuntu` / `22` |
| Checkout | `/var/www/leadgen/repo` |
| Virtualenv | `/var/www/leadgen/venv` |
| Runtime environment | `/var/www/leadgen/repo/.env` |
| Application service | `leadgen.service` |
| Gunicorn socket | `/run/leadgen/leadgen.sock` |
| Proxy service | `nginx.service` |
| Website | `https://leadgen.flexgcc.com` |
| Pre-migration backups | `/var/www/leadgen/backups/` |

The workflow uses the existing services and environment. It does not replace systemd/Nginx configuration, create application users, import leads, restart PostgreSQL, or use Docker, ECR, SSM, or AWS access keys.

## Required GitHub Actions repository secrets

Add these under **Settings → Secrets and variables → Actions → Repository secrets**:

| Secret | Value |
|---|---|
| `EC2_IP` | The server's reachable public IPv4 address or DNS hostname only. No `ubuntu@`, URL prefix, port, or whitespace. |
| `EC2_SSH` | The complete dedicated deployment SSH private key, including BEGIN/END lines. Its public key must be authorized for `ubuntu`. Use a key without a passphrase for unattended Actions. |

Only these two secrets are used. No Actions variables or `EC2_KNOWN_HOSTS` secret are required. The environment named `EC2_IP`, if present in GitHub settings, is not used: `EC2_IP` here is a repository **secret**. Runtime database, Django and Google OAuth secrets stay on EC2 in `.env` and must not be copied into the workflow.

As requested, the runner scans the host key at the beginning of each run and uses that temporary known-hosts file for SSH. This is trust-on-first-use for each run, not independent server-identity verification: a man-in-the-middle present during the scan is not detected. Pinning a separately verified host key would be stronger but is not enabled in this two-secret setup. Never paste private keys or access tokens into chat, source files, or logs.

GitHub-hosted runners must be able to reach this public host on port 22. The private EC2 hostname/IP (`172.31.*`) is not routable from those runners. Have the sysadmin configure an appropriate restricted network path; do not expose SSH broadly as a troubleshooting shortcut.

## One-time server prerequisites

The `ubuntu` account must own or have write access to the existing checkout, venv, and `/var/www/leadgen` (for protected backups). Do not recursively change ownership of runtime data just to make a deployment pass. Confirm available disk space for dependencies, static files and a full database backup.

Install missing packages as a sysadmin, not during every deployment:

```bash
sudo apt-get update
sudo apt-get install -y git python3-venv python3-pip postgresql-client nginx curl util-linux
```

`pg_dump` must be at least as new as the PostgreSQL server's major version; install the matching client if the Ubuntu default is older. `pg_restore` must be able to read its output. The application database user must be allowed to read every application table for the backup and perform the application's migrations.

The existing `leadgen.service` must use `User=ubuntu`, `Group=www-data`, the checkout and venv above, the `.env` above as `EnvironmentFile`, and the Unix socket above. Keep Nginx pointed at that socket. Nginx must terminate HTTPS locally with a valid certificate for `leadgen.flexgcc.com`; the workflow tests this virtual host with TLS verification enabled.

The deployment requires passwordless sudo for exactly the commands in [leadgen.sudoers.example](leadgen.sudoers.example). If the existing Ubuntu policy already permits them, no new rule is needed. Otherwise a sysadmin can review and install this scoped rule:

```bash
sudo visudo -cf /var/www/leadgen/repo/deploy/venv/leadgen.sudoers.example
sudo install -m 0440 /var/www/leadgen/repo/deploy/venv/leadgen.sudoers.example /etc/sudoers.d/leadgen-deploy
```

Keep `.env` readable only by the service/deployment account (for example mode `0600` owned by `ubuntu`). Use shell-safe assignments compatible with both Bash and systemd. See [.env.production.example](../../.env.production.example). Required values include `DJANGO_DEBUG=False`, a strong `DJANGO_SECRET_KEY`, allowed hosts, trusted HTTPS origins, a PostgreSQL `DATABASE_URL`, Google OAuth client ID/secret, and `SYSTEM_ADMIN_EMAILS`. The pipeline never prints or rewrites `.env` and refuses to fall back to SQLite.

Authentication rate limits use `ALLAUTH_TRUSTED_PROXY_COUNT=1` by default when `DJANGO_DEBUG=False`, matching the existing Nginx → private Gunicorn Unix socket deployment. Nginx must append the real visitor address with `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`. The rightmost address is used, not client-supplied prefixes. Do not expose Gunicorn directly or copy this proxy count to a different network topology without review. Direct local development defaults to zero trusted proxies. An absent/invalid client IP fails closed; rate limits must not be disabled to work around a proxy configuration error.

This repository is currently public: the server fetches its credential-free HTTPS URL, and normalizes `origin` to that URL after a successful fetch. No GitHub PAT is needed on EC2. Revoke any token previously shared or embedded in a remote URL, and clear the old credentialed URL without printing it:

```bash
cd /var/www/leadgen/repo
git remote set-url origin https://github.com/operation-flexgcc/Leadgen_System.git
```

If the repository becomes private, configure a read-only deploy key and explicitly update this fetch strategy; do not embed a replacement PAT in the script.

## What each run does

1. **Test job:** install locked dependencies; run `pip check`, deployment syntax/safety tests, Django checks, migration-drift checks, and the full Django suite against an isolated PostgreSQL service; check production settings and collect static files.
2. **SSH:** verify the two secrets, validate the deployment key, scan the server host key, and connect as `ubuntu`.
3. **Preflight:** validate paths, production `.env`, clean tracked files, `main` branch, tools and Nginx configuration. Acquire the same non-overlapping deployment lock used by the legacy `deploy.sh`.
4. **Update:** fetch public `main`, verify the tested SHA belongs to it, and fast-forward the server to that exact SHA. This is the safe equivalent of pulling `main`, without accidentally deploying a newer untested commit. Divergent history, a stale run that would downgrade the server, or conflicting server changes fail rather than being discarded. No `reset --hard` or `git clean` is used.
5. **Prepare Django:** activate `/var/www/leadgen/venv`, load the same `.env` as Gunicorn, install `requirements.lock.txt`, run `pip check`, Django checks, migration-drift checks, production checks, and the migration plan. New migrations must be committed; production never generates migration files.
6. **Backup:** create a mode-`0600` PostgreSQL custom-format dump under the mode-`0700` backups directory, then validate its archive listing. A failed/empty/unreadable backup stops the release before migrations. This is an archive-readability check, not a full restore test.
7. **Migrate/static:** apply migrations, confirm none remain unapplied, and run `collectstatic --noinput`.
8. **Restart/verify:** restart `leadgen`; check service status and healthy JSON through its Unix socket; validate Nginx config, restart `nginx`, verify service status and the local HTTPS virtual host. PostgreSQL and unrelated services remain running.
9. **External verification:** from GitHub, require healthy `/health/` JSON, a FlexGCC login page, and HTTP 200 for the favicon over public HTTPS. This is an application check, not ICMP ping. No production users or lead records are created.
10. **Summary:** report the deployed commit and successful checks in the Actions run summary. A failed step fails the run; it is not reported as a successful deployment.

New pushes cannot cancel an active deployment mid-migration. GitHub may replace a pending run with a newer pending commit. The server lock also protects against concurrent manual deployments. Existing `.env`, SQLite files, uploads, and unrelated untracked server files are preserved. Git will refuse a merge if an untracked file conflicts with a new tracked path.

## Manual deployment and troubleshooting

Prefer rerunning the GitHub workflow. For an approved manual deployment, use the full SHA of a tested commit on `main`:

```bash
cd /var/www/leadgen/repo
bash deploy/venv/deploy-ec2.sh FULL_TESTED_MAIN_COMMIT_SHA
bash deploy/venv/check-public.sh
```

Read-only diagnostics on the server:

```bash
sudo systemctl status leadgen nginx --no-pager
sudo journalctl -u leadgen -n 100 --no-pager
sudo nginx -t
git rev-parse HEAD
```

Review logs privately; application errors can contain sensitive information. Missing secret errors are fixed in GitHub settings. SSH timeout means the public host/network path needs checking. Dirty/divergent checkout failures require preserving and reconciling server edits. Backup version or permission errors must be resolved before rerunning. Do not bypass failed migrations or health checks.

## Recovery and backup retention

Deployment is in-place, not an atomic zero-downtime release. A failed command stops subsequent work but does not undo an earlier Git fast-forward, package install, or migration. Services are restarted only after migration/static preparation succeeds. The failure log identifies the stage. An old running process is not proof that the new release succeeded.

Prefer a tested forward-fix or revert commit on `main`, then rerun this pipeline. Do not automatically reverse migrations or restore a database: schema compatibility and writes made since the backup require a sysadmin's review. Keep off-server/RDS backups and point-in-time recovery where available, and test restoration separately. The local pre-migration backup does not include uploads or PostgreSQL cluster roles. Backups are never automatically deleted by this workflow; the sysadmin must monitor disk space, archive them securely, and set an appropriate retention policy.

The root `deploy.sh`, older generic service/sudoers examples, and `deploy/aws/` are legacy/manual alternatives, not the active production path. Older handover documents may describe those alternatives.
