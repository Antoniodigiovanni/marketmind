"""Deployment invariants for the image-only Linux installation."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest
from dotenv import dotenv_values

from agent.catchup import MAX_CATCHUP_DAYS, missing_dates

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.prod.yml"
MANAGED_DATABASE = ROOT / "docker-compose.managed-db.yml"
RUNBOOK = ROOT / "deploy" / "README.md"
SYSTEMD = ROOT / "deploy" / "systemd"


def test_production_image_pins_python_and_uv() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12.14-slim" in dockerfile
    assert "uv==0.12.7" in dockerfile
    assert 'ENTRYPOINT ["/app/docker-entrypoint.sh"]' in dockerfile
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
    assert "umask 077" in entrypoint


def test_build_context_excludes_credentials_and_local_artifacts() -> None:
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for pattern in (".env*", "secrets/", "AGENTS.md", "reports/", ".git/"):
        assert pattern in ignored
    assert "!.env.example" in ignored
    assert "deploy/" not in ignored


def test_postgres_is_the_only_structured_store() -> None:
    assert not (ROOT / "data/db/schema.sql").exists()
    assert not (ROOT / "data/db/init.py").exists()
    for path in (ROOT / "pyproject.toml", ROOT / "uv.lock", COMPOSE):
        assert "duckdb" not in path.read_text(encoding="utf-8").lower()


def test_production_dsn_uses_shared_database_hostname() -> None:
    dsn = dotenv_values(ROOT / ".env.example")["MARKETMIND_DATABASE_DSN"]
    assert dsn is not None
    assert urlparse(dsn).hostname == "postgres-shared"
    assert urlparse(dsn).path == "/marketmind"
    assert urlparse(dsn).password is None


def test_compose_uses_digest_inputs_and_file_secrets() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    for variable in (
        "MARKETMIND_IMAGE",
        "MARKETMIND_UID",
        "MARKETMIND_GID",
    ):
        assert f"${{{variable}:?" in compose
    assert "env_file:" not in compose
    assert "PGPASSWORD:" not in compose
    assert "POSTGRES_PASSWORD:" not in compose
    assert "PGPASSFILE: /run/secrets/app_pgpass" in compose
    assert "MARKETMIND_MCP_TOKEN_FILE: /run/secrets/mcp_token" in compose
    assert "AI_API_KEY_FILE: /run/secrets/llm_api_key" in compose
    assert "FRED_API_KEY_FILE: /run/secrets/fred_api_key" in compose
    assert "./data/raw:/app/data/raw" in compose
    assert "./state:/var/lib/marketmind" in compose
    assert "read_only: true" in compose
    assert "build:" not in compose
    assert compose.count("image: ${MARKETMIND_IMAGE:?") == 2
    assert "  postgres:" not in compose
    assert "depends_on:" not in compose
    assert "external: true" in compose


def test_managed_database_is_an_explicit_override() -> None:
    managed = MANAGED_DATABASE.read_text(encoding="utf-8")
    assert "image: ${MARKETMIND_POSTGRES_IMAGE:?" in managed
    assert "POSTGRES_USER: postgres" in managed
    assert "POSTGRES_PASSWORD_FILE: /run/secrets/postgres_admin_password" in managed
    assert "marketmind_database_password" in managed
    assert "postgres_data:/var/lib/postgresql/data" in managed
    assert "init-marketmind-db.sh:/docker-entrypoint-initdb.d/10-marketmind.sh:ro" in managed
    assert "POSTGRES_USER: marketmind" not in managed
    assert managed.count("depends_on:") == 2


def test_resolved_compose_modes_select_one_database_path() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is unavailable")
    env = {
        **os.environ,
        "MARKETMIND_IMAGE": "ghcr.io/example/marketmind@sha256:" + "a" * 64,
        "MARKETMIND_POSTGRES_IMAGE": "pgvector/pgvector@sha256:" + "b" * 64,
        "MARKETMIND_DATABASE_DSN": "postgresql://marketmind@postgres-shared:5432/marketmind",
        "MARKETMIND_DATABASE_NETWORK": "postgres-network",
        "MARKETMIND_UID": "1000",
        "MARKETMIND_GID": "1000",
        "SEC_CONTACT_NAME": "Test",
        "SEC_CONTACT_EMAIL": "test@example.com",
    }

    def services(*files: Path) -> dict[str, object]:
        command = ["docker", "compose"]
        for path in files:
            command.extend(("-f", str(path)))
        command.extend(("config", "--format", "json"))
        result = subprocess.run(command, check=True, capture_output=True, text=True, env=env)
        return json.loads(result.stdout)["services"]

    external_services = services(COMPOSE)
    assert set(external_services) == {"marketmind"}
    assert "depends_on" not in external_services["marketmind"]

    managed_services = services(COMPOSE, MANAGED_DATABASE)
    assert set(managed_services) == {"marketmind", "postgres"}
    assert "postgres" in managed_services["marketmind"]["depends_on"]


def test_release_bundle_and_runbook_need_no_source_checkout() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    for item in ("steps.image.outputs.digest", "release.env", "sha256sum", "gh release create"):
        assert item in workflow
    assert "docker-compose.prod.yml docker-compose.managed-db.yml" in workflow
    assert "deploy/systemd" in workflow
    assert "sha256sum --check" in runbook
    assert "git clone" not in runbook
    assert "docker create" not in runbook


def test_dashboard_is_loopback_and_has_only_read_credentials() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    dashboard = compose.split("  dashboard:", maxsplit=1)[1].split("\nsecrets:", maxsplit=1)[0]
    assert '"127.0.0.1:${MARKETMIND_DASHBOARD_PORT:-8001}' in dashboard
    assert "read_only: true" in dashboard
    assert "./state:/var/lib/marketmind:ro" in dashboard
    assert "MARKETMIND_DASHBOARD_TOKEN_FILE" in dashboard
    assert "PGPASSFILE: /run/secrets/dashboard_pgpass" in dashboard
    assert "MARKETMIND_DATABASE_DSN" not in dashboard
    assert "AI_API_KEY_FILE" not in dashboard
    assert 'profiles: ["dashboard"]' in dashboard


def test_resolved_dashboard_environment_excludes_write_credentials() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is unavailable")
    env = {
        **os.environ,
        "MARKETMIND_IMAGE": "ghcr.io/example/marketmind@sha256:" + "a" * 64,
        "MARKETMIND_DATABASE_DSN": "postgresql://marketmind@postgres-shared:5432/marketmind",
        "MARKETMIND_DATABASE_NETWORK": "postgres-network",
        "MARKETMIND_UID": "1000",
        "MARKETMIND_GID": "1000",
        "SEC_CONTACT_NAME": "Test",
        "SEC_CONTACT_EMAIL": "test@example.com",
        "MARKETMIND_DASHBOARD_DSN": ("postgresql://marketmind_ro@postgres-shared:5432/marketmind"),
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "dashboard",
            "-f",
            str(COMPOSE),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    services = json.loads(result.stdout)["services"]
    dashboard = services["dashboard"]
    dashboard_env = dashboard["environment"]
    assert dashboard_env["MARKETMIND_DASHBOARD_DSN"] == env["MARKETMIND_DASHBOARD_DSN"]
    assert dashboard_env["PGPASSFILE"] == "/run/secrets/dashboard_pgpass"
    assert {item["source"] for item in dashboard["secrets"]} == {
        "dashboard_pgpass",
        "dashboard_token",
    }
    for forbidden in (
        "MARKETMIND_DATABASE_DSN",
        "PGPASSWORD",
        "AI_API_KEY",
        "MARKETMIND_MCP_TOKEN",
    ):
        assert forbidden not in dashboard_env


def test_dashboard_reads_only_the_run_audit_log() -> None:
    allowed = {"data.db.pool", "data.db.runs", "data.secrets"}
    for path in (ROOT / "dashboard").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        imported = set(re.findall(r"^from (data\.[\w.]+) import", source, flags=re.MULTILINE))
        assert imported <= allowed


def test_timers_and_shared_job_runner() -> None:
    runner = (ROOT / "deploy/run-job.sh").read_text(encoding="utf-8")
    assert "flock -n" in runner
    assert "python -m agent.catchup" in runner
    assert "python -m data.filings.backfill --financials" in runner
    assert "python -m data.filings.esef" in runner
    assert "initial-backfill.complete" in runner
    assert "data.filings.backfill --issuers" in runner
    assert "--frozen --no-sync" in runner
    for name in ("daily", "universe", "profiles", "financials", "esef", "backup"):
        service = (SYSTEMD / f"marketmind-{name}.service").read_text(encoding="utf-8")
        timer = (SYSTEMD / f"marketmind-{name}.timer").read_text(encoding="utf-8")
        assert f"run-job.sh {name}" in service
        assert "OnCalendar=" in timer
        assert "Persistent=true" in timer
    assert "*-02,05,08,11-01 04:00:00" in (SYSTEMD / "marketmind-financials.timer").read_text(
        encoding="utf-8"
    )
    assert "*-*-01 05:00:00" in (SYSTEMD / "marketmind-esef.timer").read_text(encoding="utf-8")
    assert "MARKETMIND_BACKUP_REMOTE" in (ROOT / "deploy/backup-set.sh").read_text(encoding="utf-8")
    for script in (
        "run-job.sh",
        "backup-set.sh",
        "restore-check.sh",
        "init-marketmind-db.sh",
    ):
        subprocess.run(["sh", "-n", str(ROOT / "deploy" / script)], check=True)


def test_interactive_installer_discovers_owner_and_configures_schedules() -> None:
    installer_path = ROOT / "deploy/install.sh"
    installer = installer_path.read_text(encoding="utf-8")
    subprocess.run(["bash", "-n", str(installer_path)], check=True)

    assert "id -un" in installer
    assert "id -u" in installer
    assert "id -g" in installer
    assert "antonio" not in installer.lower()
    assert 'print "User=" user' in installer
    assert 'print "Group=" group' in installer
    assert "Run this as your normal deployment account" in installer
    assert "systemd-analyze calendar" in installer
    assert "installed_schedule" in installer
    assert "systemctl is-enabled" in installer
    assert "systemctl start --no-block marketmind-initial-backfill.service" in installer
    assert "marketmind-initial-backfill.timer" in installer
    assert "*-*-* 06:10:00" in installer
    assert "*-02,05,08,11-01 04:00:00" in installer
    assert "BACKFILL_MODE=scheduled" in installer

    backfill_service = (SYSTEMD / "marketmind-initial-backfill.service").read_text(encoding="utf-8")
    assert "run-job.sh backfill" in backfill_service


def test_backup_is_one_verified_offsite_set() -> None:
    backup = (ROOT / "deploy/backup-set.sh").read_text(encoding="utf-8")
    restore = (ROOT / "deploy/restore-check.sh").read_text(encoding="utf-8")
    for marker in ("pg_dump", "files.tar.gz", "SHA256SUMS", "rclone copy", "rclone check"):
        assert marker in backup
    assert "docker compose -f docker-compose.prod.yml exec" not in backup
    assert 'jq -er ".networks.database.name"' not in backup
    assert ".networks.database.name" in backup
    assert "pg_restore" in restore
    assert "POSTGRES_HOST_AUTH_METHOD=trust" in restore
    assert "sha256sum --check" in restore
    assert "marketmind-backup.timer" in RUNBOOK.read_text(encoding="utf-8")


def test_catchup_is_bounded_and_ordered() -> None:
    today = dt.date(2026, 9, 14)
    assert missing_dates(today, today) == []
    assert missing_dates(today - dt.timedelta(days=2), today) == [
        today - dt.timedelta(days=1),
        today,
    ]
    assert missing_dates(None, today) == [today]
    with pytest.raises(RuntimeError, match="exceed"):
        missing_dates(today - dt.timedelta(days=MAX_CATCHUP_DAYS + 1), today)


def test_dashboard_role_and_private_logs_documented() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert "GRANT SELECT ON agent_runs TO marketmind_ro" in runbook
    assert "ssh -N -L 8001:127.0.0.1:8001" in runbook
    assert "tailscale serve --bg http://127.0.0.1:8001" in runbook
    logrotate = (ROOT / "deploy/logrotate").read_text(encoding="utf-8")
    assert "create 0600 root root" in logrotate


def test_initial_filings_backfills_are_documented_before_timers() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    schedule = runbook.index("### Install the schedule")
    assert runbook.index("data.filings.backfill --issuers") < schedule
    assert runbook.index("data.filings.backfill --financials") < schedule
    assert runbook.index("python -m data.filings.esef") < schedule


def test_installer_is_the_primary_documented_deployment_path() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    installer = runbook.index("./deploy/install.sh")
    manual = runbook.index("## Manual installation reference")
    assert installer < manual
    assert "next 22:00 local time" in runbook
    assert "Installation does\nnot wait for the backfill" in runbook
