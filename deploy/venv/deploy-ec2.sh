#!/usr/bin/env bash
# Invoked over SSH by deploy-on-ec2.yml, or manually with a tested main SHA.
set -Eeuo pipefail
umask 022

stage=preflight
trap 'echo "Deployment failed during: $stage (line $LINENO). No automatic code/database rollback was attempted." >&2' ERR
die() { echo "$*" >&2; exit 1; }

readonly app_dir=/var/www/leadgen/repo
readonly venv_dir=/var/www/leadgen/venv
readonly backup_dir=/var/www/leadgen/backups
readonly canonical_repo=https://github.com/operation-flexgcc/Leadgen_System.git
readonly expected_sha="${1:-}"
[[ "$expected_sha" =~ ^[0-9a-f]{40}$ ]] || die 'Pass the full tested main commit SHA.'
[[ "$(id -un)" == ubuntu ]] || die 'Run this deployment as ubuntu, not root.'
[[ -d "$app_dir/.git" ]] || die "Missing checkout: $app_dir"
[[ -x "$venv_dir/bin/python" && -r "$venv_dir/bin/activate" ]] || die "Missing virtualenv: $venv_dir"
[[ -r "$app_dir/.env" ]] || die "Missing protected production environment: $app_dir/.env"
for executable in git flock curl pg_dump pg_restore sudo; do
  command -v "$executable" > /dev/null || die "Required server executable is missing: $executable"
done

# Share the lock with the legacy manual deploy.sh; never overlap migrations.
exec 9>/tmp/flexgcc-outreach-deploy.lock
flock -n 9 || die 'Another deployment is already running.'
cd "$app_dir"
[[ "$(git symbolic-ref --short HEAD)" == main ]] || die 'Server checkout must be on main.'
git diff --quiet && git diff --cached --quiet || die 'Tracked server changes found; preserve/reconcile them before deploying.'
[[ -z "$(git ls-files -- .env)" ]] || die '.env must never be tracked in Git.'

# This is the exact .env read by leadgen.service. Never run Django without it.
# shellcheck disable=SC1091
source "$venv_dir/bin/activate"
set -a
# shellcheck disable=SC1091
source "$app_dir/.env"
set +a
readonly python_bin="$venv_dir/bin/python"
for name in DJANGO_SECRET_KEY DJANGO_ALLOWED_HOSTS DJANGO_CSRF_TRUSTED_ORIGINS DATABASE_URL GOOGLE_OAUTH_CLIENT_ID GOOGLE_OAUTH_CLIENT_SECRET SYSTEM_ADMIN_EMAILS; do
  [[ -n "${!name:-}" && "${!name}" != *REPLACE* ]] || die "Missing/placeholder production setting: $name"
done
debug_value="$(printf '%s' "${DJANGO_DEBUG:-}" | tr '[:upper:]' '[:lower:]')"
case "$debug_value" in
  false|0|no|off) ;;
  *) die 'Set DJANGO_DEBUG=False in the server .env before deploying.' ;;
esac
[[ "$DATABASE_URL" == postgresql://* || "$DATABASE_URL" == postgres://* ]] || die 'Production DATABASE_URL must use PostgreSQL; refusing SQLite fallback.'
sudo -n /usr/sbin/nginx -t

stage='fast-forward to the tested main commit'
# Public repository: do not use, log, or depend on a PAT embedded in origin.
git fetch --no-tags "$canonical_repo" main
git remote set-url origin "$canonical_repo"
git cat-file -e "$expected_sha^{commit}"
git merge-base --is-ancestor "$expected_sha" FETCH_HEAD || die 'Requested commit is not on remote main.'
git merge-base --is-ancestor HEAD "$expected_sha" || die 'Refusing a downgrade or divergent server history. Deploy a new main commit instead.'
git merge --ff-only "$expected_sha"
[[ "$(git rev-parse HEAD)" == "$expected_sha" ]] || die 'Server HEAD does not match the tested commit.'

stage='install pinned requirements'
"$python_bin" -m pip install --disable-pip-version-check --requirement requirements.lock.txt
"$python_bin" -m pip check

stage='Django configuration and migration checks'
"$python_bin" manage.py check
"$python_bin" manage.py makemigrations --check --dry-run
"$python_bin" manage.py check --deploy --fail-level ERROR
"$python_bin" manage.py migrate --plan

stage='PostgreSQL backup before migrations'
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
backup_file="$(mktemp "$backup_dir/pre-${expected_sha:0:12}-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX.dump")"
# Parse DATABASE_URL into libpq environment variables, never argv/logs.
# The dump stays mode 0600, outside the checkout; never upload it to Actions.
"$python_bin" deploy/venv/backup_database.py "$backup_file"
[[ -s "$backup_file" ]] || die 'Database backup is empty; refusing migrations.'
pg_restore --list "$backup_file" > /dev/null
echo "Pre-migration backup saved: $backup_file"

stage='apply migrations and collect static files'
"$python_bin" manage.py migrate --noinput
"$python_bin" manage.py migrate --check
"$python_bin" manage.py collectstatic --noinput

stage='restart leadgen and Nginx'
sudo -n /usr/bin/systemctl restart leadgen
sudo -n /usr/bin/systemctl is-active --quiet leadgen

check_health() {
  local label="$1"
  shift
  local attempt
  for attempt in {1..30}; do
    if curl --fail --silent --show-error --connect-timeout 5 --max-time 10 "$@" \
      | "$python_bin" -c 'import json, sys
try:
    healthy = json.load(sys.stdin).get("status") == "ok"
except (ValueError, AttributeError):
    healthy = False
sys.exit(0 if healthy else 1)'; then
      echo "$label: healthy"
      return 0
    fi
    sleep 2
  done
  echo "$label failed after 30 attempts." >&2
  return 1
}

stage='Gunicorn socket health check'
check_health 'Gunicorn socket' --unix-socket /run/leadgen/leadgen.sock \
  --header 'Host: leadgen.flexgcc.com' --header 'X-Forwarded-Proto: https' http://localhost/health/

stage='restart and check Nginx'
sudo -n /usr/sbin/nginx -t
sudo -n /usr/bin/systemctl restart nginx
sudo -n /usr/bin/systemctl is-active --quiet nginx
# Exercise the real local HTTPS virtual host, including certificate verification.
check_health 'Local Nginx HTTPS' --resolve leadgen.flexgcc.com:443:127.0.0.1 https://leadgen.flexgcc.com/health/

echo "Deployed and verified main commit $expected_sha. PostgreSQL was not restarted."
