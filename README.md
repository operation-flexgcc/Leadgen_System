# FlexGCC Outreach

FlexGCC Outreach is a Google-authenticated sales prospect and follow-up tracker for interns, managers, and system administrators.

## What is implemented

- Prospect records: company, website, description, contact name, LinkedIn, email, and phone.
- Up to five outreach records per prospect, each with medium (phone, email, or LinkedIn), date, and response.
- Next action, next action date, status, comments, and conditional meeting date/time.
- Statuses: Not yet responded, Not interested, Meeting to be scheduled, Meeting scheduled, and Meeting done.
- Dashboard with 20 prospects per page and filters for today's actions, status, outreach count, assigned intern, overdue/upcoming actions, and search.
- Google OAuth login with exact-email pre-provisioning and optional Google-domain restriction.
- Three extensible user classes:

| Class | Access |
|---|---|
| Sales intern | Sees and updates only assigned prospects |
| Manager | Sees and updates all intern prospects |
| System admin | Has manager visibility and adds, edits, disables, restores, or deletes users |

Deleting an unused user removes the account. If the user owns prospect or outreach history, the app disables access and retains the account as an audit reference.

## Local setup with Docker

Requirements: Docker Engine with Docker Compose v2 and a Google OAuth web client.

```bash
cp .env.example .env
# Add GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, and your email to SYSTEM_ADMIN_EMAILS.
docker compose --env-file .env up --build
```

Open `http://localhost:8000`.

Configure the local Google client with:

- Authorized JavaScript origin: `http://localhost:8000`
- Authorized redirect URI: `http://localhost:8000/accounts/google/login/callback/`

The email in `SYSTEM_ADMIN_EMAILS` can sign in first. Add interns and managers from **Users** before they sign in.

## Local setup without Docker

Use Python 3.13 or 3.14 and PostgreSQL for production parity. SQLite is used only when `DATABASE_URL` is absent.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
```

## Validation

```bash
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py check
.venv/bin/python manage.py test
.venv/bin/python manage.py collectstatic --noinput
docker build -t flexgcc-outreach:local .
```

## Runtime configuration

| Variable | Required in production | Description |
|---|---:|---|
| `DJANGO_SECRET_KEY` | Yes | Long random application secret |
| `DJANGO_ALLOWED_HOSTS` | Yes | Comma-separated hostnames |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Yes | Comma-separated HTTPS origins |
| `DATABASE_URL` | Yes | PostgreSQL connection URL |
| `GOOGLE_OAUTH_CLIENT_ID` | Yes | Google OAuth web client ID |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Yes | Google OAuth client secret |
| `GOOGLE_ALLOWED_DOMAINS` | Recommended | Optional comma-separated Google domains |
| `REQUIRE_PREPROVISIONED_USERS` | Recommended | Defaults to `True`; rejects unknown email addresses |
| `SYSTEM_ADMIN_EMAILS` | Bootstrap | Comma-separated initial system admins |
| `MANAGER_EMAILS` | Optional | Comma-separated manager bootstrap emails |
| `DJANGO_TIME_ZONE` | No | Defaults to `Asia/Kolkata` |

Production must use `DJANGO_DEBUG=False` and HTTPS. Never commit `.env` or OAuth/database credentials.

The current native EC2 deployment for `leadgen.flexgcc.com` is documented in [`deploy/venv/README.md`](deploy/venv/README.md). The production workflow must load `.env` before it runs any `manage.py` command; otherwise migrations can silently target Django's local SQLite fallback instead of PostgreSQL.

## User operations

- A system admin adds a name, exact Google email, and user class at `/system/users/`.
- A system admin can change a user's name, email, or class.
- System admins cannot remove their own admin role or delete themselves.
- The last active system admin cannot be downgraded or deleted.
- `python manage.py set_user_role EMAIL intern|manager|system_admin` is available for emergency server-side role recovery after the user has signed in once.

## Architecture

The application uses Django 5.2 LTS, PostgreSQL, server-rendered responsive templates, django-allauth for Google OAuth, Gunicorn, and WhiteNoise. Prospect history is normalized into related outreach records; user classes live in an extensible profile model rather than being hard-coded into page logic.

See [`deploy/aws/README.md`](deploy/aws/README.md) for the exact AWS architecture, one-time setup, GitHub variables, deployment behavior, rollback, and first-deployment verification.

For a server running Django directly in an activated Python virtualenv, use the complete [production environment template](.env.production.example), root [`deploy.sh`](deploy.sh), and [`deploy/venv/README.md`](deploy/venv/README.md). This path installs dependencies, applies migrations, collects static files, restarts Gunicorn through systemd, and verifies application health.
