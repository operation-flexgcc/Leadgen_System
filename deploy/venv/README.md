# Native virtualenv deployment

Use this path when the AWS/Linux server runs Django directly in a Python virtualenv instead of Docker. The application server is Gunicorn managed by systemd; Nginx or an AWS Application Load Balancer terminates public HTTP/HTTPS traffic.

## Current LeadGen production layout

The live `leadgen.flexgcc.com` pipeline uses these paths and names:

```text
Checkout:       /var/www/leadgen/repo
Virtualenv:     /var/www/leadgen/venv
Environment:    /var/www/leadgen/repo/.env
Systemd service: leadgen
Gunicorn socket: /run/leadgen/leadgen.sock
```

The checked-in `.github/workflows/deploy-production.yml` loads that `.env` before every Django command, so migrations and Gunicorn always use the same PostgreSQL database. For a manual deployment, use:

```bash
cd /var/www/leadgen/repo
source /var/www/leadgen/venv/bin/activate
./deploy.sh .env
```

The production `.env` must include the `FLEXGCC_*` values from `.env.production.example` so `deploy.sh` restarts the `leadgen` service and checks its Unix socket.

## One-time server setup

The examples assume Ubuntu, source at `/opt/flexgcc-outreach`, virtualenv at `/opt/flexgcc-outreach/venv`, service user `flexgcc`, and public hostname `outreach.YOUR_DOMAIN`.

Install the operating-system packages:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip postgresql-client nginx curl util-linux
```

Create a dedicated service account, clone the private repository, and create the virtualenv:

```bash
id -u flexgcc >/dev/null 2>&1 || sudo useradd --system --create-home --home-dir /var/lib/flexgcc --shell /usr/sbin/nologin flexgcc
sudo mkdir -p /opt/flexgcc-outreach
sudo chown flexgcc:www-data /opt/flexgcc-outreach
sudo -u flexgcc git clone git@github.com:FlexGCC/leadgen.git /opt/flexgcc-outreach
sudo -u flexgcc python3 -m venv /opt/flexgcc-outreach/venv
sudo -u flexgcc mkdir -p /opt/flexgcc-outreach/staticfiles
sudo chown flexgcc:www-data /opt/flexgcc-outreach/staticfiles
sudo chmod 2750 /opt/flexgcc-outreach/staticfiles
```

Because the repository is private, configure a read-only GitHub deploy key for the `flexgcc` account before cloning. A GitHub App token with HTTPS is also acceptable. Do not store a personal access token in the repository or environment file.

Create the production environment file:

```bash
sudo install -m 0600 -o flexgcc -g www-data /opt/flexgcc-outreach/.env.production.example /etc/flexgcc-outreach.env
sudoedit /etc/flexgcc-outreach.env
```

Replace every `REPLACE_*` value. Generate the Django secret with `openssl rand -hex 32`. URL-encode special characters in the PostgreSQL password. The Google OAuth redirect URI must be exactly:

```text
https://outreach.YOUR_DOMAIN/accounts/google/login/callback/
```

Set `GOOGLE_ALLOWED_DOMAINS` to a comma-separated domain list, or leave it empty when approved users use mixed domains. Exact-email pre-provisioning remains enforced by `REQUIRE_PREPROVISIONED_USERS=True`.

Install and enable Gunicorn:

```bash
sudo install -m 0644 /opt/flexgcc-outreach/deploy/venv/flexgcc-outreach.service.example /etc/systemd/system/flexgcc-outreach.service
sudo systemctl daemon-reload
sudo systemctl enable flexgcc-outreach.service
```

If the paths or Linux user differ, edit the unit before enabling it. Verify that `systemctl` and `journalctl` are in `/usr/bin`, then install the narrowly scoped sudo rule used by `deploy.sh`:

```bash
sudo visudo -cf /opt/flexgcc-outreach/deploy/venv/sudoers.example
sudo install -m 0440 /opt/flexgcc-outreach/deploy/venv/sudoers.example /etc/sudoers.d/flexgcc-outreach
```

This permits the `flexgcc` user to restart and inspect only the FlexGCC Outreach service. Do not grant unrestricted passwordless sudo.

Optional Nginx configuration:

```bash
sudo install -m 0644 /opt/flexgcc-outreach/deploy/venv/nginx.conf.example /etc/nginx/sites-available/flexgcc-outreach
sudo ln -s /etc/nginx/sites-available/flexgcc-outreach /etc/nginx/sites-enabled/flexgcc-outreach
sudo nginx -t
sudo systemctl reload nginx
```

Replace `outreach.REPLACE_DOMAIN` first. Use Certbot or an AWS ALB/ACM certificate for HTTPS. Do not expose Gunicorn port 8000 publicly.

The supplied service binds Gunicorn to `127.0.0.1:8000` for Nginx on the same server. If an AWS ALB connects directly to Gunicorn, change the bind address to `0.0.0.0:8000` and restrict the instance security group so only the ALB security group can reach that port.

## Deploy or update

The CI/CD runner or sysadmin should update the checkout before running the deployment script:

```bash
cd /opt/flexgcc-outreach
git fetch origin main
git checkout main
git pull --ff-only origin main
source /opt/flexgcc-outreach/venv/bin/activate
./deploy.sh /etc/flexgcc-outreach.env
```

`deploy.sh` is intentionally idempotent. It:

1. Refuses to run without an active virtualenv or complete production environment.
2. Prevents concurrent deployments with `flock`.
3. Installs the pinned Python dependencies and runs `pip check`.
4. Checks Django configuration and confirms no migration is missing from Git.
5. Collects static files.
6. Optionally runs tests when `FLEXGCC_RUN_TESTS=True`.
7. Prints the migration plan and applies all migrations non-interactively.
8. Runs Django production deployment checks.
9. Restarts the Gunicorn systemd service.
10. Polls `/health/` and fails with recent service logs if the app is unhealthy.

Keep `FLEXGCC_RUN_TESTS=False` for routine production deployments because GitHub CI already runs the suite. Enabling it requires the PostgreSQL application user to be allowed to create a temporary Django test database.

## First-deployment verification

```bash
sudo systemctl status flexgcc-outreach --no-pager
curl -H 'Host: outreach.YOUR_DOMAIN' http://127.0.0.1:8000/health/
```

The expected response is `{"status":"ok"}`. Then verify Google login, create one intern and one manager from **Users**, and test prospect visibility for each class.

## Rollback

Application rollback:

```bash
cd /opt/flexgcc-outreach
git log --oneline -10
git checkout PREVIOUS_GOOD_COMMIT
source venv/bin/activate
./deploy.sh /etc/flexgcc-outreach.env
```

Keep RDS automated backups and point-in-time recovery enabled. Take an explicit RDS snapshot before any future destructive migration; an application-code rollback does not reverse database schema changes.
