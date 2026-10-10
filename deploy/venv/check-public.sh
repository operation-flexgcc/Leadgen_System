#!/usr/bin/env bash
set -Eeuo pipefail

readonly website=https://leadgen.flexgcc.com
for attempt in {1..12}; do
  if curl --fail --silent --show-error --connect-timeout 5 --max-time 15 \
    --header 'Cache-Control: no-cache' "$website/health/" \
    | python3 -c 'import json, sys
try:
    healthy = json.load(sys.stdin).get("status") == "ok"
except (ValueError, AttributeError):
    healthy = False
sys.exit(0 if healthy else 1)'; then
    break
  fi
  if [[ "$attempt" == 12 ]]; then
    echo 'Public HTTPS /health/ did not return healthy JSON.' >&2
    exit 1
  fi
  sleep 5
done

# A healthy DB endpoint alone does not prove templates/static URLs are working.
login_page="$(curl --fail --silent --show-error --connect-timeout 5 --max-time 20 "$website/login/")"
[[ "$login_page" == *FlexGCC* ]] || { echo 'Public login page is not the expected FlexGCC page.' >&2; exit 1; }
favicon_status="$(curl --silent --show-error --connect-timeout 5 --max-time 20 --output /dev/null --write-out '%{http_code}' "$website/static/images/flexgcc-favicon.png")"
[[ "$favicon_status" == 200 ]] || { echo "Public favicon returned HTTP $favicon_status, expected 200." >&2; exit 1; }
echo 'Public HTTPS health, FlexGCC login page, and favicon passed.'
