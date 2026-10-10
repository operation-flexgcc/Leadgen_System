"""Isolated deployment control-flow tests. Never connect to a real server/database."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
MOCK = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
command = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["DEPLOY_TEST_LOG"], "a") as output:
    output.write(json.dumps([command, *args]) + "\n")
scenario = os.environ.get("DEPLOY_TEST_SCENARIO", "success")
if command == "id":
    print("ubuntu")
elif command == "git":
    if args[0] == "symbolic-ref":
        print("main")
    elif args[0] == "diff" and scenario == "dirty":
        sys.exit(1)
    elif args[0] == "merge-base" and scenario == "downgrade" and args[2] == "HEAD":
        sys.exit(1)
    elif args[0] == "rev-parse":
        print("a" * 40)
elif command == "pg_dump":
    # Assert the private connection URL was passed only via the environment.
    assert os.environ["PGDATABASE"].startswith("postgresql://")
    assert not any("password" in value and value != "--no-password" for value in args)
    if scenario == "backup_failure":
        sys.exit(1)
    destination = next(value.split("=", 1)[1] for value in args if value.startswith("--file="))
    pathlib.Path(destination).write_bytes(b"mock-postgres-backup")
elif command == "pg_restore" and scenario == "invalid_backup":
    sys.exit(1)
elif command == "python":
    if args[:3] == ["manage.py", "migrate", "--noinput"] and scenario == "migration_failure":
        sys.exit(1)
    if args[0] == "-c":
        # Execute the actual health-response parser, without importing the app.
        exec(args[1])
elif command == "sudo":
    if "nginx" in args[1] and scenario == "invalid_nginx":
        sys.exit(1)
elif command == "curl":
    if scenario == "unhealthy":
        print('{"status": "error"}')
    elif scenario == "health_html":
        print("<html>Login</html>")
    elif args[-1].endswith("/login/"):
        if scenario == "wrong_login":
            print("Other website")
        else:
            print("<html>FlexGCC</html>")
    elif "--write-out" in args:
        print("404" if scenario == "missing_favicon" else "200", end="")
    else:
        print('{"status": "ok"}')
'''


class EC2DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="leadgen-deploy-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "repo"
        self.app.mkdir()
        (self.app / ".git").mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ["git", "flock", "curl", "pg_dump", "pg_restore", "sudo", "id", "sleep"]:
            path = self.bin / name
            path.write_text(MOCK)
            path.chmod(0o755)
        self.venv = self.root / "venv" / "bin"
        self.venv.mkdir(parents=True)
        (self.venv / "activate").write_text("# Isolated test virtualenv\n")
        (self.venv / "python").write_text(MOCK)
        (self.venv / "python").chmod(0o755)
        self.env_text = "\n".join([
            "DJANGO_DEBUG=False", "DJANGO_SECRET_KEY=test-only-secret",
            "DJANGO_ALLOWED_HOSTS=leadgen.flexgcc.com",
            "DJANGO_CSRF_TRUSTED_ORIGINS=https://leadgen.flexgcc.com",
            "DATABASE_URL=postgresql://test:private-password@localhost/test",
            "GOOGLE_OAUTH_CLIENT_ID=test-client", "GOOGLE_OAUTH_CLIENT_SECRET=test-secret",
            "SYSTEM_ADMIN_EMAILS=test@example.com",
        ])
        (self.app / ".env").write_text(self.env_text)
        (self.app / "db.sqlite3").write_bytes(b"preserve-local-database")
        (self.app / "untracked.txt").write_text("preserve-server-file")
        self.log = self.root / "commands.jsonl"
        self.env = {
            **os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
            "DEPLOY_TEST_LOG": str(self.log),
        }
        script = (ROOT / "deploy/venv/deploy-ec2.sh").read_text()
        script = script.replace("/var/www/leadgen", str(self.root))
        script = script.replace("/tmp/flexgcc-outreach-deploy.lock", str(self.root / "deploy.lock"))
        self.script = self.root / "deploy.sh"
        self.script.write_text(script)

    def run_deploy(self, scenario="success", sha=SHA):
        return subprocess.run(
            ["bash", str(self.script), sha], env={**self.env, "DEPLOY_TEST_SCENARIO": scenario},
            text=True, capture_output=True, timeout=30,
        )

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_successful_deploy_order_and_data_preservation(self):
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = self.commands()
        def position(prefix):
            return next(i for i, row in enumerate(commands) if row[:len(prefix)] == prefix)
        self.assertLess(position(["git", "merge", "--ff-only", SHA]), position(["python", "-m", "pip", "install"]))
        self.assertLess(position(["python", "manage.py", "check"]), position(["pg_dump"]))
        self.assertLess(position(["pg_restore"]), position(["python", "manage.py", "migrate", "--noinput"]))
        self.assertLess(position(["python", "manage.py", "collectstatic"]), position(["sudo", "-n", "/usr/bin/systemctl", "restart", "leadgen"]))
        self.assertLess(position(["sudo", "-n", "/usr/bin/systemctl", "restart", "leadgen"]), position(["sudo", "-n", "/usr/bin/systemctl", "restart", "nginx"]))
        self.assertEqual((self.app / ".env").read_text(), self.env_text)
        self.assertEqual((self.app / "db.sqlite3").read_bytes(), b"preserve-local-database")
        self.assertEqual((self.app / "untracked.txt").read_text(), "preserve-server-file")
        backups = list((self.root / "backups").glob("*.dump"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertNotIn("private-password", result.stdout + result.stderr + self.log.read_text())
        self.assertNotIn(["sudo", "-n", "/usr/bin/systemctl", "restart", "postgresql"], commands)

    def test_missing_or_invalid_commit_stops_before_commands(self):
        for sha in ["", "main", "abc; touch /tmp/unsafe"]:
            self.assertNotEqual(self.run_deploy(sha=sha).returncode, 0)
        self.assertEqual(self.commands(), [])

    def test_missing_environment_stops_before_git_fetch(self):
        (self.app / ".env").unlink()
        self.assertNotEqual(self.run_deploy().returncode, 0)
        self.assertFalse(any(row[:2] == ["git", "fetch"] for row in self.commands()))

    def test_sqlite_is_rejected_before_dependencies_or_migrations(self):
        (self.app / ".env").write_text(self.env_text.replace("postgresql://test:private-password@localhost/test", "sqlite:///db.sqlite3"))
        result = self.run_deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing SQLite fallback", result.stderr)
        self.assertFalse(any(row[0] == "python" for row in self.commands()))

    def test_missing_debug_is_rejected_with_explanation(self):
        (self.app / ".env").write_text(self.env_text.replace("DJANGO_DEBUG=False", "unset DJANGO_DEBUG"))
        result = self.run_deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Set DJANGO_DEBUG=False", result.stderr)

    def test_dirty_checkout_is_not_overwritten(self):
        self.assertNotEqual(self.run_deploy("dirty").returncode, 0)
        self.assertFalse(any(row[:2] == ["git", "fetch"] for row in self.commands()))

    def test_downgrade_is_rejected_before_git_merge(self):
        self.assertNotEqual(self.run_deploy("downgrade").returncode, 0)
        self.assertFalse(any(row[:2] == ["git", "merge"] for row in self.commands()))

    def test_nginx_configuration_failure_stops_before_git_update(self):
        self.assertNotEqual(self.run_deploy("invalid_nginx").returncode, 0)
        self.assertFalse(any(row[:2] == ["git", "fetch"] for row in self.commands()))

    def test_backup_failure_stops_migrations(self):
        for scenario in ["backup_failure", "invalid_backup"]:
            with self.subTest(scenario=scenario):
                self.assertNotEqual(self.run_deploy(scenario).returncode, 0)
        self.assertNotIn(["python", "manage.py", "migrate", "--noinput"], self.commands())

    def test_failed_migration_does_not_restart_services(self):
        self.assertNotEqual(self.run_deploy("migration_failure").returncode, 0)
        self.assertFalse(any("restart" in row for row in self.commands()))

    def test_failed_socket_health_does_not_restart_nginx(self):
        self.assertNotEqual(self.run_deploy("unhealthy").returncode, 0)
        self.assertNotIn(["sudo", "-n", "/usr/bin/systemctl", "restart", "nginx"], self.commands())

    def test_public_checks_reject_unhealthy_json_html_wrong_app_or_missing_icon(self):
        for scenario in ["success", "unhealthy", "health_html", "wrong_login", "missing_favicon"]:
            with self.subTest(scenario=scenario):
                result = subprocess.run(
                    ["bash", str(ROOT / "deploy/venv/check-public.sh")],
                    env={**self.env, "DEPLOY_TEST_SCENARIO": scenario},
                    text=True, capture_output=True, timeout=30,
                )
                self.assertEqual(result.returncode == 0, scenario == "success", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
