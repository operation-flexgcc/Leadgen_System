#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

trap 'echo "Deployment failed at line $LINENO." >&2' ERR

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"

if [[ -n "${1:-}" ]]; then
  env_file="$1"
elif [[ -n "${FLEXGCC_ENV_FILE:-}" ]]; then
  env_file="$FLEXGCC_ENV_FILE"
elif [[ -r /etc/flexgcc-outreach.env ]]; then
  env_file=/etc/flexgcc-outreach.env
else
  env_file="$project_dir/.env"
fi
if [[ ! -r "$env_file" ]]; then
  echo "Cannot read production environment file: $env_file" >&2
  echo "Pass its path to deploy.sh, set FLEXGCC_ENV_FILE, or create a protected .env file." >&2
  exit 1
fi

if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  echo "No Python virtualenv is active." >&2
  echo "Activate the deployment virtualenv before running this script." >&2
  exit 1
fi

python_bin="$VIRTUAL_ENV/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo "Active virtualenv does not contain an executable Python: $python_bin" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$env_file"
set +a

deploy_lock_file="${FLEXGCC_DEPLOY_LOCK_FILE:-/tmp/flexgcc-outreach-deploy.lock}"
if ! command -v flock >/dev/null 2>&1; then
  echo "flock is required to prevent concurrent deployments (package: util-linux)." >&2
  exit 1
fi
exec 9>"$deploy_lock_file"
if ! flock -n 9; then
  echo "Another FlexGCC Outreach deployment is already running." >&2
  exit 1
fi

required_variables=(
  DJANGO_SECRET_KEY
  DJANGO_ALLOWED_HOSTS
  DJANGO_CSRF_TRUSTED_ORIGINS
  DATABASE_URL
  GOOGLE_OAUTH_CLIENT_ID
  GOOGLE_OAUTH_CLIENT_SECRET
  SYSTEM_ADMIN_EMAILS
)
for required_variable in "${required_variables[@]}"; do
  required_value="${!required_variable:-}"
  if [[ -z "$required_value" ]]; then
    echo "Required environment variable is empty: $required_variable" >&2
    exit 1
  fi
  if [[ "$required_value" == *REPLACE* ]]; then
    echo "Replace the placeholder value for $required_variable before deploying." >&2
    exit 1
  fi
done

debug_value="$(printf '%s' "${DJANGO_DEBUG:-}" | tr '[:upper:]' '[:lower:]')"
if [[ "$debug_value" != "false" && "$debug_value" != "0" && "$debug_value" != "no" && "$debug_value" != "off" ]]; then
  echo "DJANGO_DEBUG must be False in production." >&2
  exit 1
fi
if [[ "$DATABASE_URL" != postgresql://* && "$DATABASE_URL" != postgres://* ]]; then
  echo "DATABASE_URL must point to PostgreSQL in production." >&2
  exit 1
fi
if [[ "${GOOGLE_ALLOWED_DOMAINS:-}" == *REPLACE* || "${FLEXGCC_HEALTHCHECK_HOST:-}" == *REPLACE* ]]; then
  echo "Replace the remaining domain placeholders in the production environment file." >&2
  exit 1
fi

echo "Using Python: $python_bin"
echo "Installing pinned Python dependencies..."
"$python_bin" -m pip install --disable-pip-version-check --requirement requirements.lock.txt
"$python_bin" -m pip check

echo "Checking Django configuration and committed migrations..."
"$python_bin" manage.py check
"$python_bin" manage.py makemigrations --check --dry-run

echo "Collecting production static files..."
"$python_bin" manage.py collectstatic --noinput

run_tests_value="$(printf '%s' "${FLEXGCC_RUN_TESTS:-False}" | tr '[:upper:]' '[:lower:]')"
if [[ "$run_tests_value" == "true" || "$run_tests_value" == "1" || "$run_tests_value" == "yes" || "$run_tests_value" == "on" ]]; then
  echo "Running application test suite..."
  "$python_bin" manage.py test --noinput
fi

echo "Reviewing and applying database migrations..."
"$python_bin" manage.py migrate --plan
"$python_bin" manage.py migrate --noinput

echo "Running Django production deployment checks..."
"$python_bin" manage.py check --deploy --fail-level ERROR

restart_value="$(printf '%s' "${FLEXGCC_RESTART_SERVICE:-True}" | tr '[:upper:]' '[:lower:]')"
service_name="${FLEXGCC_SERVICE_NAME:-flexgcc-outreach}"
if [[ "$restart_value" == "true" || "$restart_value" == "1" || "$restart_value" == "yes" || "$restart_value" == "on" ]]; then
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl is required when FLEXGCC_RESTART_SERVICE=True." >&2
    exit 1
  fi
  echo "Restarting Gunicorn service: $service_name"
  if [[ "$EUID" -eq 0 ]]; then
    systemctl restart "$service_name"
    systemctl is-active --quiet "$service_name"
  else
    sudo -n systemctl restart "$service_name"
    sudo -n systemctl is-active --quiet "$service_name"
  fi
else
  echo "Service restart skipped because FLEXGCC_RESTART_SERVICE=False."
fi

healthcheck_url="${FLEXGCC_HEALTHCHECK_URL:-http://127.0.0.1:8000/health/}"
healthcheck_host="${FLEXGCC_HEALTHCHECK_HOST:-}"
healthcheck_unix_socket="${FLEXGCC_HEALTHCHECK_UNIX_SOCKET:-}"
healthcheck_attempts="${FLEXGCC_HEALTHCHECK_ATTEMPTS:-30}"
healthcheck_delay="${FLEXGCC_HEALTHCHECK_DELAY_SECONDS:-2}"
healthcheck_ok=false

echo "Waiting for application health: $healthcheck_url"
for attempt in $(seq 1 "$healthcheck_attempts"); do
  curl_arguments=(--fail --silent --show-error --max-time 5)
  if [[ -n "$healthcheck_host" ]]; then
    curl_arguments+=(--header "Host: $healthcheck_host")
  fi
  if [[ -n "$healthcheck_unix_socket" ]]; then
    curl_arguments+=(--unix-socket "$healthcheck_unix_socket")
  fi
  if curl "${curl_arguments[@]}" "$healthcheck_url" >/dev/null; then
    healthcheck_ok=true
    break
  fi
  sleep "$healthcheck_delay"
done

if [[ "$healthcheck_ok" != "true" ]]; then
  echo "Deployment completed, but the health check did not pass." >&2
  if command -v journalctl >/dev/null 2>&1; then
    if [[ "$EUID" -eq 0 ]]; then
      journalctl --unit "$service_name" --lines 100 --no-pager >&2 || true
    else
      sudo -n journalctl --unit "$service_name" --lines 100 --no-pager >&2 || true
    fi
  fi
  exit 1
fi

echo "FlexGCC Outreach deployment completed successfully."
