#!/usr/bin/env bash
set -euo pipefail

image_uri="${1:?Usage: deploy.sh ECR_IMAGE_URI AWS_REGION}"
aws_region="${2:?Usage: deploy.sh ECR_IMAGE_URI AWS_REGION}"
deployment_dir="/opt/flexgcc-outreach"
compose_file="$deployment_dir/compose.prod.yml"
env_file="/etc/flexgcc-outreach.env"
container_name="flexgcc-outreach-web"

if [[ ! -f "$compose_file" ]]; then
  echo "Missing $compose_file. Complete the one-time EC2 setup first." >&2
  exit 1
fi
if [[ ! -f "$env_file" ]]; then
  echo "Missing $env_file. Install the production environment file first." >&2
  exit 1
fi

registry="${image_uri%%/*}"
previous_image="$(docker inspect --format '{{.Config.Image}}' "$container_name" 2>/dev/null || true)"

aws ecr get-login-password --region "$aws_region" | docker login --username AWS --password-stdin "$registry"

export IMAGE_URI="$image_uri"
docker compose --env-file "$env_file" -f "$compose_file" pull web
docker compose --env-file "$env_file" -f "$compose_file" run --rm web python manage.py migrate --plan
docker compose --env-file "$env_file" -f "$compose_file" run --rm web python manage.py migrate --noinput
docker compose --env-file "$env_file" -f "$compose_file" up -d --no-deps --remove-orphans web

healthy=false
for attempt in $(seq 1 30); do
  if curl --fail --silent --show-error http://127.0.0.1:8000/health/ >/dev/null; then
    healthy=true
    break
  fi
  sleep 2
done

if [[ "$healthy" != "true" ]]; then
  docker logs --tail 150 "$container_name" >&2 || true
  if [[ -n "$previous_image" && "$previous_image" != "$image_uri" ]]; then
    echo "Health check failed. Restoring previous application image $previous_image." >&2
    export IMAGE_URI="$previous_image"
    docker compose --env-file "$env_file" -f "$compose_file" up -d --no-deps web
  fi
  exit 1
fi

echo "FlexGCC Outreach is healthy on image $image_uri"
