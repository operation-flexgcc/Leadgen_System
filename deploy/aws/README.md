# AWS production handoff

## Recommended v1 architecture

Use a small, conventional stack that can grow without re-platforming:

- Route 53 DNS and an ACM certificate.
- An internet-facing Application Load Balancer terminating HTTPS.
- One EC2 instance running the immutable application container. Put the instance in a private subnet if outbound NAT is available; otherwise lock its security group to ALB traffic on port 8000 and use Systems Manager instead of SSH.
- Amazon RDS for PostgreSQL with encryption, automated backups, and point-in-time recovery.
- Amazon ECR for application images.
- AWS Systems Manager for deployment commands and logs.
- GitHub Actions authenticated to AWS through OIDC, not long-lived AWS access keys.

For higher availability later, move the same image and environment contract to ECS/Fargate and run at least two tasks across Availability Zones. The application code and database schema do not need to change.

## One-time AWS setup

1. Create an ECR repository, for example `flexgcc-outreach`.
2. Create PostgreSQL on RDS. Create a dedicated application database/user and enable automated backups with a retention period appropriate to the business.
3. Launch an SSM-managed EC2 instance with Docker Engine, Docker Compose v2, AWS CLI v2, and `curl` installed.
4. Attach an instance role containing `AmazonSSMManagedInstanceCore` plus read access to the one ECR repository.
5. Configure the ALB target group on HTTP port 8000 with health path `/health/`. Allow the EC2 security group to receive port 8000 only from the ALB security group.
6. Copy `compose.prod.yml` and `deploy/aws/deploy.sh` to the instance:

   ```bash
   sudo mkdir -p /opt/flexgcc-outreach
   sudo install -m 0644 compose.prod.yml /opt/flexgcc-outreach/compose.prod.yml
   sudo install -m 0755 deploy/aws/deploy.sh /opt/flexgcc-outreach/deploy.sh
   ```

7. Copy the canonical `.env.production.example` to `/etc/flexgcc-outreach.env`, fill every value, then restrict it:

   ```bash
   sudo install -m 0600 .env.production.example /etc/flexgcc-outreach.env
   sudoedit /etc/flexgcc-outreach.env
   ```

8. Create an AWS IAM role trusted by GitHub's OIDC provider. Restrict the trust policy to this repository, the `main` branch, and preferably the GitHub `production` environment. Grant only ECR push permissions for the repository and `ssm:SendCommand`/command-status permissions for this EC2 instance.

## GitHub configuration

Create a protected GitHub environment named `production`. Add an approval rule if production changes need human review. Add these repository or environment variables:

| Variable | Example | Purpose |
|---|---|---|
| `AWS_REGION` | `ap-south-1` | AWS region for ECR and EC2 |
| `AWS_ROLE_ARN` | `arn:aws:iam::123456789012:role/github-flexgcc-deploy` | OIDC deployment role |
| `ECR_REPOSITORY` | `flexgcc-outreach` | ECR repository name |
| `EC2_INSTANCE_ID` | `i-0123456789abcdef0` | SSM-managed server |

The deployment job deliberately skips until all four values exist. No AWS access key or database/OAuth secret belongs in GitHub. Runtime secrets live only in `/etc/flexgcc-outreach.env` (or may later move to Secrets Manager).

## Google OAuth configuration

Create a Google OAuth client of type **Web application**. Configure:

- Authorized JavaScript origin: `https://YOUR_DOMAIN`
- Authorized redirect URI: `https://YOUR_DOMAIN/accounts/google/login/callback/`

The URI must match exactly, including HTTPS and the trailing slash. Put the client ID and secret only in `/etc/flexgcc-outreach.env`.

Set `SYSTEM_ADMIN_EMAILS` to the first system administrator before the first sign-in. After that person signs in, they can add all other approved users in **Users**. With `REQUIRE_PREPROVISIONED_USERS=True`, unknown Google accounts are rejected even when they belong to an allowed domain.

## Deployment contract

Every push to `main` runs CI and, once AWS variables exist, the production workflow:

1. Builds one Docker image from the tested commit.
2. Pushes immutable `<git-sha>` and convenience `latest` tags to ECR.
3. Uses SSM to make the EC2 instance pull the immutable tag.
4. Prints the database migration plan and applies migrations before replacing the app container.
5. Polls `/health/` for up to 60 seconds.
6. Restores the previous application image if the new container fails health checks.

The workflow fails unless SSM reports success. The deployed image URI remains visible in the workflow output for audit and rollback.

## Rollback and data safety

Roll back application code by rerunning the server script with a previous ECR image URI:

```bash
sudo /opt/flexgcc-outreach/deploy.sh ACCOUNT.dkr.ecr.REGION.amazonaws.com/flexgcc-outreach:PREVIOUS_GIT_SHA REGION
```

Database migrations must be backward-compatible with the previous application image. Before any future destructive migration, take an RDS snapshot and test both migration and restore in staging. Keep RDS point-in-time recovery enabled. The v1 migration only creates new tables and indexes.

## Verification after first deployment

1. `https://YOUR_DOMAIN/health/` returns `{"status":"ok"}`.
2. The bootstrap system admin can sign in with Google.
3. The system admin can add an intern and manager by exact Google email.
4. The intern sees only their assigned prospect.
5. The manager sees both interns' prospects and can filter the team dashboard.
6. Setting status to **Meeting scheduled** requires a date and time.
7. A sixth outreach record is rejected.

Before enabling `DJANGO_SECURE_HSTS_PRELOAD`, confirm that every current and future subdomain will remain HTTPS-only. HSTS preload is intentionally off by default because browser preload removal is slow and operationally difficult to reverse.

## Common failures

- `redirect_uri_mismatch`: the Google callback does not exactly match `https://DOMAIN/accounts/google/login/callback/`.
- `DisallowedHost`: add the hostname to `DJANGO_ALLOWED_HOSTS`.
- CSRF failure after Google login or form submission: add the HTTPS origin to `DJANGO_CSRF_TRUSTED_ORIGINS` and confirm the ALB forwards `X-Forwarded-Proto: https`.
- User sees “A system administrator must add…”: add the exact email in **Users**, or bootstrap it through `SYSTEM_ADMIN_EMAILS`.
- Deployment job is skipped: one or more required GitHub variables is absent.
- SSM command does not start: confirm the instance is online in Systems Manager and both IAM roles have the scoped SSM/ECR permissions.
