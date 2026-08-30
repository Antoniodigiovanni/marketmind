"""Deployment invariants that must hold on a fresh or migrated Linux host."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = ROOT / "docker-compose.prod.yml"
CRONTAB_PATH = ROOT / "deploy" / "crontab"
DEPLOY_README_PATH = ROOT / "deploy" / "README.md"
LOGROTATE_PATH = ROOT / "deploy" / "logrotate"
DOCKERFILE_PATH = ROOT / "Dockerfile"
ENTRYPOINT_PATH = ROOT / "docker-entrypoint.sh"
FILE_BACKUP_PATH = ROOT / "deploy" / "backup-files.sh"
DOCKERIGNORE_PATH = ROOT / ".dockerignore"


def test_production_image_pins_python_and_uv() -> None:
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")

    assert "FROM python:3.12.14-slim" in dockerfile
    assert "uv==0.12.7" in dockerfile
    assert 'ENTRYPOINT ["/app/docker-entrypoint.sh"]' in dockerfile
    assert "umask 077" in ENTRYPOINT_PATH.read_text(encoding="utf-8")


def test_docker_context_excludes_secrets_and_local_artifacts() -> None:
    ignored = DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines()

    assert ".env*" in ignored
    assert "AGENTS.md" in ignored
    assert ".coverage" in ignored


def test_production_dsn_uses_the_compose_database_hostname() -> None:
    values = dotenv_values(ROOT / ".env.example")
    dsn = values["MARKETMIND_DATABASE_DSN"]

    assert dsn is not None
    assert urlparse(dsn).hostname == "postgres"
    assert urlparse(dsn).password is None


def test_compose_requires_secrets_and_host_ownership_mapping() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")

    for variable in (
        "MARKETMIND_DATABASE_DSN",
        "MARKETMIND_DB_PASSWORD",
        "MARKETMIND_MCP_TOKEN",
        "MARKETMIND_UID",
        "MARKETMIND_GID",
    ):
        assert f"${{{variable}:?" in compose
    assert "PGPASSWORD:" in compose
    assert "MARKETMIND_DUCKDB_PATH: /var/lib/marketmind/research.duckdb" in compose
    assert "MARKETMIND_PROCESSED_DIR: /var/lib/marketmind/processed" in compose
    assert "MARKETMIND_UNIVERSE_DIR: /var/lib/marketmind/universe" in compose
    assert "./data/raw:/app/data/raw" in compose
    assert "./data:/app/data" not in compose
    assert "./state:/var/lib/marketmind" in compose
    assert 'user: "${MARKETMIND_UID:' in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:\n      - ALL" in compose
    assert '"127.0.0.1:5432:5432"' not in compose


def test_cron_uv_invocations_never_resync_at_run_time() -> None:
    lines = CRONTAB_PATH.read_text(encoding="utf-8").splitlines()
    assert "SHELL=/bin/sh" in lines
    assert "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" in lines

    commands = [line for line in lines if line and not line.startswith("#")]

    assert commands
    job_commands = [command for command in commands if "docker compose" in command][:3]
    assert len(job_commands) == 3
    assert all("flock -n /run/lock/marketmind-data.lock" in command for command in job_commands)

    for command in commands:
        if "uv run" not in command:
            continue
        assert "--frozen" in command
        assert "--no-sync" in command


def test_cron_backups_create_the_destination_and_escape_percent_signs() -> None:
    commands = [
        line
        for line in CRONTAB_PATH.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    backup_commands = [line for line in commands if "/srv/marketmind-backups" in line]

    assert len(backup_commands) == 1
    assert "mkdir -p /srv/marketmind-backups" in backup_commands[0]
    assert "umask 077" in backup_commands[0]
    assert 'mv "$' in backup_commands[0] and '.tmp"' in backup_commands[0]
    assert all(re.search(r"(?<!\\)%", line) is None for line in commands)
    assert all(">> /var/log/marketmind/" in line for line in commands if "docker" in line)


def test_file_backup_stops_the_service_and_verifies_the_archive() -> None:
    script = FILE_BACKUP_PATH.read_text(encoding="utf-8")

    assert "stop marketmind" in script
    assert "start marketmind" in script
    assert "trap restart_service EXIT" in script
    assert "trap 'exit 1' HUP INT TERM" in script
    assert "data/raw state" in script
    assert "tar -tzf" in script


def test_cron_command_quoting_is_valid_posix_shell() -> None:
    lines = CRONTAB_PATH.read_text(encoding="utf-8").splitlines()
    scheduled_commands = [
        line.split(maxsplit=6)[6]
        for line in lines
        if line and not line.startswith("#") and not line.startswith(("SHELL=", "PATH="))
    ]

    assert len(scheduled_commands) == 5
    for command in scheduled_commands:
        subprocess.run(["sh", "-n", "-c", command], check=True)

    subprocess.run(["sh", "-n", str(FILE_BACKUP_PATH)], check=True)


def test_existing_install_runbook_prevents_duplicate_schedules() -> None:
    runbook = DEPLOY_README_PATH.read_text(encoding="utf-8")

    assert "Migrating an existing systemd install" in runbook
    assert "systemctl disable --now" in runbook
    assert "marketmind-daily.timer" in runbook
    assert "pg_restore" in runbook
    assert "/etc/cron.d/marketmind" in runbook
    assert "sudo crontab deploy/crontab" not in runbook


def test_scheduled_logs_are_private_and_rotated() -> None:
    config = LOGROTATE_PATH.read_text(encoding="utf-8")
    runbook = DEPLOY_README_PATH.read_text(encoding="utf-8")

    assert "/var/log/marketmind/*.log" in config
    assert "rotate 14" in config
    assert "create 0600 root root" in config
    assert "install -d -m 0700 -o root -g root" in runbook
    assert "/srv/marketmind-backups /var/log/marketmind" in runbook
