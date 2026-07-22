# FlexGCC Outreach

FlexGCC Outreach is a Google-authenticated partner-outreach tracker for sales interns, inside sales, founder-LinkedIn operators, managers, and system administrators.

## What is implemented

- Separate prospect histories per outreach workstream, with qualification evidence, consulting focus, client segment, senior-contact details, and within-workstream duplicate suppression.
- Three playbook workstreams: sales intern, inside sales, and LinkedIn outreach through either Kandarp Soni or Sunit Kala.
- Up to five outreach records per prospect, each with playbook activity, medium, date, and response or factual notes.
- Playbook stages from Research required and Eligible through Founder meeting booked, plus next actions, outcome status, comments, and complete meeting handoff details.
- Statuses: Not yet responded, Not interested, Meeting to be scheduled, Meeting scheduled, and Meeting done.
- Dashboard with 20 prospects per page and manager filters for user class, assigned operator, playbook stage, outcome status, outreach count, today's actions, and overdue/upcoming actions.
- Immutable system-generated company UUIDs shared across that company's workstream records.
- Role-scoped company downloads in CSV and formatted XLSX with `ID`, `Name`, `Location`, and `URL` columns.
- Rotating API credentials, audited company-detail updates, and atomic prospect claiming by company ID.
- Google OAuth login with exact-email pre-provisioning and optional Google-domain restriction.
- Five extensible user classes:

| Class | Access |
|---|---|
| Sales intern | Qualifies firms, originates outreach, and sees assigned prospects plus its unassigned role queue |
| Inside sales | Runs multi-channel cadence and sees assigned prospects plus its unassigned role queue |
| LinkedIn outreach | Operates one founder account per prospect and sees assigned prospects plus its unassigned role queue |
| Manager | Sees and updates all prospects across all three outreach classes |
| System admin | Has manager visibility and adds, edits, disables, restores, or deletes users |

Deleting an unused user removes the account. If the user owns prospect or outreach history, the app disables access and retains the account as an audit reference.

## Approved target-firm import

The repository contains the approved Florida/Chicago and NY/MA/CT source lists under `data/target_firms/`. The import is idempotent and creates unassigned records at the **Research required** stage:

- Florida/Chicago: 80 sales-intern records and 80 LinkedIn-outreach records.
- NY/MA/CT: 90 inside-sales records and 90 LinkedIn-outreach records.
- Total: 340 workstream records from 170 source firms.

Run a rolled-back rehearsal first, then apply:

```bash
python manage.py import_target_firms
python manage.py import_target_firms --apply --created-by-email admin@flexgcc.com
```

Frontline users see unassigned prospects only in their own user class. They claim a prospect, complete company/contact research, and save it as Eligible before the app permits outreach. Managers can assign unassigned prospects directly from the edit screen. The manual **Import target firms** GitHub workflow runs the same command against production.

The migration assigns one company UUID to all existing records with the same normalized website domain. The ID remains stable even when company details are later updated.

## Company API

Every logged-in user can open **API access** and generate:

- a short-lived bearer access token;
- a single-use refresh token that is stored only as a SHA-256 hash and rotates whenever it is used.

Send the access token as `Authorization: Bearer <access_token>`. Refresh a token pair with:

```http
POST /api/v1/token/refresh/
Content-Type: application/json

{"refresh_token":"<refresh_token>"}
```

Update company details by immutable company ID:

```http
PATCH /api/v1/companies/<company_id>/
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "short_description": "...",
  "consulting_focus": "...",
  "client_segment": "...",
  "eligibility_evidence": "...",
  "contact_name": "...",
  "contact_title": "...",
  "linkedin_url": "https://www.linkedin.com/in/...",
  "email": "optional@example.com",
  "phone": "+1 ..."
}
```

`GET` on the same URL returns the company details. Managers and system administrators can read or update every company. Frontline users must own the prospect for their workstream. Each successful update is stored with the authenticated user, prior values, new values, and timestamp.

Claim the prospect for the token user's own workstream with:

```http
POST /api/v1/companies/<company_id>/claim/
Authorization: Bearer <access_token>
```

If another user has claimed it, the API returns HTTP `409` and the existing user's name.

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

The email in `SYSTEM_ADMIN_EMAILS` can sign in first. Add outreach users and managers from **Users** before they sign in.

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
| `API_ACCESS_TOKEN_MINUTES` | No | Access-token lifetime; defaults to `15` |
| `API_REFRESH_TOKEN_DAYS` | No | Refresh-token lifetime; defaults to `30` |
| `API_TOKEN_ISSUER` | No | Token issuer; defaults to `flexgcc-outreach` |
| `API_TOKEN_AUDIENCE` | No | Token audience; defaults to `flexgcc-outreach-api` |
| `API_TOKEN_SIGNING_KEY` | Recommended | Separate long random signing secret; falls back to `DJANGO_SECRET_KEY` |

Production must use `DJANGO_DEBUG=False` and HTTPS. Never commit `.env` or OAuth/database credentials.

The current native EC2 deployment for `leadgen.flexgcc.com` is documented in [`deploy/venv/README.md`](deploy/venv/README.md). The production workflow must load `.env` before it runs any `manage.py` command; otherwise migrations can silently target Django's local SQLite fallback instead of PostgreSQL.

## User operations

- A system admin adds a name, exact Google email, and user class at `/system/users/`.
- A system admin can change a user's name, email, or class.
- System admins cannot remove their own admin role or delete themselves.
- The last active system admin cannot be downgraded or deleted.
- `python manage.py set_user_role EMAIL intern|inside_sales|linkedin_outreach|manager|system_admin` is available for emergency server-side role recovery after the user has signed in once.

## Architecture

The application uses Django 5.2 LTS, PostgreSQL, server-rendered responsive templates, django-allauth for Google OAuth, Gunicorn, and WhiteNoise. Prospect history is normalized into related outreach records; user classes live in an extensible profile model rather than being hard-coded into page logic.

See [`deploy/aws/README.md`](deploy/aws/README.md) for the exact AWS architecture, one-time setup, GitHub variables, deployment behavior, rollback, and first-deployment verification.

For a server running Django directly in an activated Python virtualenv, use the complete [production environment template](.env.production.example), root [`deploy.sh`](deploy.sh), and [`deploy/venv/README.md`](deploy/venv/README.md). This path installs dependencies, applies migrations, collects static files, restarts Gunicorn through systemd, and verifies application health.
