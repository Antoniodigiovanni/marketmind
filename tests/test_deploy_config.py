"""Deployment invariants for the image-only Linux installation."""

from __future__ import annotations

import datetime as dt
import hashlib
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
SHARED_DATABASE = ROOT / "docker-compose.postgres.yml"
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


def test_shared_database_has_an_independent_lifecycle() -> None:
    shared = SHARED_DATABASE.read_text(encoding="utf-8")
    assert "name: shared-postgres" in shared
    assert "image: ${POSTGRES_IMAGE:?" in shared
    assert "POSTGRES_USER: postgres" in shared
    assert "POSTGRES_PASSWORD_FILE: /run/secrets/postgres_admin_password" in shared
    assert "postgres_data:/var/lib/postgresql/data" in shared
    assert "postgres-shared" in shared
    assert "external: true" in shared
    assert "marketmind_database_password" not in shared
    assert "init-marketmind-db.sh" not in shared
    assert "depends_on:" not in shared
    assert "ports:" not in shared


def test_application_and_shared_database_are_separate_compose_projects() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is unavailable")
    env = {
        **os.environ,
        "MARKETMIND_IMAGE": "ghcr.io/example/marketmind@sha256:" + "a" * 64,
        "POSTGRES_IMAGE": "pgvector/pgvector@sha256:" + "b" * 64,
        "POSTGRES_NETWORK": "postgres-network",
        "POSTGRES_DATA_VOLUME": "postgres-shared-data",
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

    shared_services = services(SHARED_DATABASE)
    assert set(shared_services) == {"postgres"}
    assert "marketmind" not in shared_services


def test_release_bundle_and_runbook_need_no_source_checkout() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    runbook = RUNBOOK.read_text(encoding="utf-8")
    for item in ("steps.image.outputs.digest", "release.env", "sha256sum", "gh release create"):
        assert item in workflow
    assert "docker-compose.prod.yml docker-compose.postgres.yml" in workflow
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
        "provision-marketmind-db.sh",
        "provision-dashboard-db.sh",
    ):
        subprocess.run(["sh", "-n", str(ROOT / "deploy" / script)], check=True)
    for script in ("dashboard.sh", "marketmind.sh", "update.sh"):
        subprocess.run(["bash", "-n", str(ROOT / "deploy" / script)], check=True)


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
    assert "POSTGRES_INSTALL_DIR=/srv/postgres" in installer
    assert "docker-compose.postgres.yml" in installer
    assert "provision-marketmind-db.sh" in installer
    assert '"$INSTALL_DIR/deploy/dashboard.sh" enable' in installer
    assert '"$INSTALL_DIR/deploy/dashboard.sh" disable' in installer
    assert "docker-compose.managed-db.yml" not in installer
    assert "*-*-* 06:10:00" in installer
    assert "*-02,05,08,11-01 04:00:00" in installer
    assert "BACKFILL_MODE=scheduled" in installer
    assert '[[ "${SELECTED_ENABLED[backup]}" == yes ]] || return 0' in installer
    assert "The main freshness job" in installer
    assert "It does not\\nrun the daily price/news pipeline" in installer
    assert "model-provider intensive" in installer
    assert "Quarterly\\nmatches the reporting cycle" in installer
    assert "Requires jq, rclone" in installer
    assert 'prompt_yes_no "Enable $label?"' in installer
    for package_manager in ("apt-get", "dnf", "yum", "zypper", "pacman"):
        assert package_manager in installer
    assert 'install_system_packages "${missing[@]}"' in installer
    assert "rclone listremotes" in installer
    assert "rclone config" in installer
    assert "rclone lsd" in installer
    assert 'prompt_yes_no "Enable the operations dashboard?"' in installer
    assert 'prompt_yes_no "Expose the dashboard privately through Tailscale Serve?"' in installer
    assert '"$INSTALL_DIR/deploy/dashboard.sh" tailscale enable' in installer

    backfill_service = (SYSTEMD / "marketmind-initial-backfill.service").read_text(encoding="utf-8")
    assert "run-job.sh backfill" in backfill_service


def test_installer_can_skip_unconfigured_backups() -> None:
    bash_major = int(
        subprocess.run(
            ["bash", "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    if bash_major < 4:
        pytest.skip("installer requires Bash 4+; macOS ships Bash 3")
    installer_path = ROOT / "deploy/install.sh"
    env = {**os.environ, "INSTALLER_PATH": str(installer_path)}
    subprocess.run(
        [
            "bash",
            "-c",
            'source "$INSTALLER_PATH"; SELECTED_ENABLED[backup]=no; configure_backup',
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )


def test_installer_uses_detected_package_manager_for_backup_dependencies(
    tmp_path: Path,
) -> None:
    bash_major = int(
        subprocess.run(
            ["bash", "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    if bash_major < 4:
        pytest.skip("installer requires Bash 4+; macOS ships Bash 3")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "sudo-calls"
    fake_apt = fake_bin / "apt-get"
    fake_apt.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_apt.chmod(0o755)
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$INSTALLER_CALL_LOG"\n',
        encoding="utf-8",
    )
    fake_sudo.chmod(0o755)

    installer_path = ROOT / "deploy/install.sh"
    env = {
        **os.environ,
        "INSTALLER_PATH": str(installer_path),
        "INSTALLER_CALL_LOG": str(call_log),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    subprocess.run(
        [
            "bash",
            "-c",
            'source "$INSTALLER_PATH"; install_system_packages jq rclone',
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )

    assert call_log.read_text(encoding="utf-8").splitlines() == [
        "apt-get update",
        "apt-get install -y jq rclone",
    ]


def test_installer_can_leave_dashboard_disabled(tmp_path: Path) -> None:
    bash_major = int(
        subprocess.run(
            ["bash", "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    if bash_major < 4:
        pytest.skip("installer requires Bash 4+; macOS ships Bash 3")

    install_dir = tmp_path / "marketmind"
    deploy_dir = install_dir / "deploy"
    deploy_dir.mkdir(parents=True)
    shutil.copy(ROOT / "deploy/install.sh", deploy_dir / "install.sh")
    (install_dir / "state").mkdir()
    (install_dir / "secrets").mkdir()
    (install_dir / ".env").write_text("", encoding="utf-8")
    env = {**os.environ, "INSTALLER_PATH": str(deploy_dir / "install.sh")}

    subprocess.run(
        [
            "bash",
            "-c",
            'source "$INSTALLER_PATH"; configure_dashboard; test "$DASHBOARD_ENABLED" = no',
        ],
        check=True,
        env=env,
        input="n\n",
        capture_output=True,
        text=True,
    )


def test_dashboard_command_generates_shared_credentials(tmp_path: Path) -> None:
    bash_major = int(
        subprocess.run(
            ["bash", "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    if bash_major < 4:
        pytest.skip("installer requires Bash 4+; macOS ships Bash 3")

    install_dir = tmp_path / "marketmind"
    deploy_dir = install_dir / "deploy"
    deploy_dir.mkdir(parents=True)
    shutil.copy(ROOT / "deploy/dashboard.sh", deploy_dir / "dashboard.sh")
    (install_dir / "state").mkdir()
    (install_dir / "secrets").mkdir()
    (install_dir / ".env").write_text(
        "MARKETMIND_DASHBOARD_DSN=postgresql://marketmind_ro@postgres-shared:5432/marketmind\n",
        encoding="utf-8",
    )
    env = {**os.environ, "DASHBOARD_SCRIPT": str(deploy_dir / "dashboard.sh")}

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$DASHBOARD_SCRIPT"; prepare_shared_credentials; prepare_browser_credential',
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )

    database_password = (
        (install_dir / "secrets/dashboard_database_password").read_text(encoding="utf-8").strip()
    )
    assert re.fullmatch(r"[0-9a-f]{64}", database_password)
    assert (install_dir / "secrets/dashboard_pgpass").read_text(encoding="utf-8") == (
        f"postgres-shared:5432:marketmind:marketmind_ro:{database_password}\n"
    )
    assert re.fullmatch(
        r"[0-9a-f]{64}\n", (install_dir / "secrets/dashboard_token").read_text(encoding="utf-8")
    )
    assert database_password not in result.stdout
    for filename in ("dashboard_database_password", "dashboard_pgpass", "dashboard_token"):
        assert (install_dir / f"secrets/{filename}").stat().st_mode & 0o777 == 0o600


def test_dashboard_command_manages_only_its_tailscale_mapping(tmp_path: Path) -> None:
    install_dir = tmp_path / "marketmind"
    deploy_dir = install_dir / "deploy"
    deploy_dir.mkdir(parents=True)
    shutil.copy(ROOT / "deploy/dashboard.sh", deploy_dir / "dashboard.sh")
    (install_dir / "state").mkdir()
    (install_dir / "state/dashboard-enabled").write_text("yes\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_tailscale = fake_bin / "tailscale"
    fake_tailscale.write_text(
        '#!/usr/bin/env bash\nif [[ "${1:-}" == ip ]]; then printf "100.64.0.1\\n"; fi\n',
        encoding="utf-8",
    )
    fake_tailscale.chmod(0o755)
    call_log = tmp_path / "sudo-calls"
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$TAILSCALE_CALL_LOG"\n',
        encoding="utf-8",
    )
    fake_sudo.chmod(0o755)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/usr/bin/env bash\nprintf '401'\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    env = {
        **os.environ,
        "DASHBOARD_SCRIPT": str(deploy_dir / "dashboard.sh"),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TAILSCALE_CALL_LOG": str(call_log),
    }

    subprocess.run(
        [
            "bash",
            "-c",
            'source "$DASHBOARD_SCRIPT"; enable_tailscale 8443; disable_tailscale',
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )

    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert calls[0] == ("tailscale serve --bg --https=8443 --set-path=/ http://127.0.0.1:8001")
    assert calls[1] == "tailscale serve status"
    assert calls[2] == "tailscale serve --https=8443 --set-path=/ off"
    assert (install_dir / "state/dashboard-tailscale-enabled").read_text(encoding="utf-8") == "no\n"
    assert "reset" not in "\n".join(calls)


def test_database_provisioning_keeps_password_out_of_process_arguments(
    tmp_path: Path,
) -> None:
    postgres_dir = tmp_path / "postgres"
    postgres_dir.mkdir()
    (postgres_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    password_file = tmp_path / "database-password"
    password = "a1b2c3d4"
    password_file.write_text(password, encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CAPTURE_ARGS"\ncat >> "$CAPTURE_STDIN"\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    args_file = tmp_path / "args"
    stdin_file = tmp_path / "stdin"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CAPTURE_ARGS": str(args_file),
        "CAPTURE_STDIN": str(stdin_file),
    }

    subprocess.run(
        [
            "sh",
            str(ROOT / "deploy/provision-marketmind-db.sh"),
            str(postgres_dir),
            str(password_file),
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )

    assert password not in args_file.read_text(encoding="utf-8")
    provisioned_sql = stdin_file.read_text(encoding="utf-8")
    assert password in provisioned_sql
    assert "CREATE DATABASE marketmind OWNER marketmind" in provisioned_sql
    assert "CREATE EXTENSION IF NOT EXISTS vector" in provisioned_sql


def test_dashboard_provisioning_limits_role_and_hides_password_from_arguments(
    tmp_path: Path,
) -> None:
    postgres_dir = tmp_path / "postgres"
    postgres_dir.mkdir()
    (postgres_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    password_file = tmp_path / "dashboard-password"
    password = "deadbeef01234567"
    password_file.write_text(password, encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CAPTURE_ARGS"\n'
        'case "$*" in *-Atqc*) printf "t\\n" ;; esac\n'
        'cat >> "$CAPTURE_STDIN"\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    args_file = tmp_path / "args"
    stdin_file = tmp_path / "stdin"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CAPTURE_ARGS": str(args_file),
        "CAPTURE_STDIN": str(stdin_file),
    }

    subprocess.run(
        [
            "sh",
            str(ROOT / "deploy/provision-dashboard-db.sh"),
            str(postgres_dir),
            str(password_file),
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )

    assert password not in args_file.read_text(encoding="utf-8")
    provisioned_sql = stdin_file.read_text(encoding="utf-8")
    assert password in provisioned_sql
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION" in provisioned_sql
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public" in provisioned_sql
    assert "GRANT SELECT ON TABLE public.agent_runs TO marketmind_ro" in provisioned_sql


def test_updater_verifies_release_and_preserves_runtime_files() -> None:
    updater = (ROOT / "deploy/update.sh").read_text(encoding="utf-8")
    assert "sha256sum --check" in updater
    assert "archive name does not match RELEASE_TAG" in updater
    assert "state/deployment-releases" in updater
    assert "state/dashboard-enabled" in updater
    assert "./deploy/install.sh --refresh-systemd" in updater
    assert 'stage_image "$release_source"' in updater
    assert "docker image inspect" in updater
    assert 'docker cp "$temporary_container:/app/deploy"' in updater
    assert "secrets/" not in updater
    assert "data/raw" not in updater
    assert "docker-compose.postgres.yml up" not in updater


def test_operator_command_updates_latest_release_by_default() -> None:
    command = (ROOT / "deploy/marketmind.sh").read_text(encoding="utf-8")
    installer = (ROOT / "deploy/install.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    subprocess.run(["bash", "-n", str(ROOT / "deploy/marketmind.sh")], check=True)
    assert 'exec "$INSTALL_DIR/deploy/update.sh" "${2:-latest}"' in command
    assert "/usr/local/bin/marketmind" in installer
    assert "${{ steps.registry.outputs.image }}:latest" in workflow
    assert "org.opencontainers.image.version=${{ github.ref_name }}" in workflow


def test_update_command_preserves_runtime_state_and_refreshes_services(tmp_path: Path) -> None:
    bash_major = int(
        subprocess.run(
            ["bash", "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    if bash_major < 4 or shutil.which("flock") is None:
        pytest.skip("updater requires Linux with Bash 4+ and flock")

    install_dir = tmp_path / "marketmind"
    deploy_dir = install_dir / "deploy"
    deploy_dir.mkdir(parents=True)
    shutil.copy(ROOT / "deploy/update.sh", deploy_dir / "update.sh")
    (deploy_dir / "update.sh").chmod(0o755)
    (install_dir / "state").mkdir()
    (install_dir / "state/dashboard-enabled").write_text("no\n", encoding="utf-8")
    (install_dir / "secrets").mkdir()
    (install_dir / "secrets/keep").write_text("secret\n", encoding="utf-8")
    (install_dir / "data").mkdir()
    (install_dir / "data/keep").write_text("raw\n", encoding="utf-8")
    (install_dir / ".env").write_text(
        "MARKETMIND_IMAGE=ghcr.io/example/marketmind@sha256:old\nKEEP=value\n",
        encoding="utf-8",
    )
    (install_dir / "release.env").write_text("RELEASE_TAG=v1.0.0\n", encoding="utf-8")
    for filename in (".env.example", "docker-compose.prod.yml", "docker-compose.postgres.yml"):
        (install_dir / filename).write_text("old\n", encoding="utf-8")

    bundle = tmp_path / "bundle"
    (bundle / "deploy").mkdir(parents=True)
    image = "ghcr.io/example/marketmind@sha256:" + "a" * 64
    (bundle / "release.env").write_text(
        f"MARKETMIND_IMAGE={image}\nRELEASE_TAG=v1.1.0\n",
        encoding="utf-8",
    )
    (bundle / ".env.example").write_text("new example\n", encoding="utf-8")
    for filename in ("docker-compose.prod.yml", "docker-compose.postgres.yml"):
        (bundle / filename).write_text("services: {}\n", encoding="utf-8")
    shutil.copy(ROOT / "deploy/update.sh", bundle / "deploy/update.sh")
    shutil.copy(ROOT / "deploy/marketmind.sh", bundle / "deploy/marketmind.sh")
    (bundle / "deploy/install.sh").write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" > state/systemd-refresh\n',
        encoding="utf-8",
    )
    (bundle / "deploy/dashboard.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    for script in (bundle / "deploy").iterdir():
        script.chmod(0o755)

    archive = tmp_path / "marketmind-v1.1.0-deploy.tar.gz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(bundle), "."], check=True)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    Path(f"{archive}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker-calls"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$DOCKER_LOG"\n', encoding="utf-8"
    )
    fake_docker.chmod(0o755)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/usr/bin/env bash\nprintf '401'\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DOCKER_LOG": str(docker_log),
    }

    subprocess.run(
        ["bash", str(deploy_dir / "update.sh"), str(archive)],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )

    updated_env = (install_dir / ".env").read_text(encoding="utf-8")
    assert f"MARKETMIND_IMAGE={image}" in updated_env
    assert "KEEP=value" in updated_env
    assert (install_dir / "secrets/keep").read_text(encoding="utf-8") == "secret\n"
    assert (install_dir / "data/keep").read_text(encoding="utf-8") == "raw\n"
    assert (install_dir / "state/current-release").read_text(encoding="utf-8") == "v1.1.0\n"
    assert (install_dir / "state/systemd-refresh").read_text(encoding="utf-8") == (
        "--refresh-systemd\n"
    )
    assert list((install_dir / "state/deployment-releases").glob("v1.0.0-*"))
    calls = docker_log.read_text(encoding="utf-8")
    assert f"pull {image}" in calls
    assert "up -d --wait --wait-timeout 120 marketmind" in calls


def test_update_command_can_stage_latest_release_from_registry(tmp_path: Path) -> None:
    bash_major = int(
        subprocess.run(
            ["bash", "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    if bash_major < 4 or shutil.which("flock") is None:
        pytest.skip("updater requires Linux with Bash 4+ and flock")

    install_dir = tmp_path / "marketmind"
    deploy_dir = install_dir / "deploy"
    deploy_dir.mkdir(parents=True)
    shutil.copy(ROOT / "deploy/update.sh", deploy_dir / "update.sh")
    (deploy_dir / "update.sh").chmod(0o755)
    (install_dir / "state").mkdir()
    (install_dir / "state/dashboard-enabled").write_text("no\n", encoding="utf-8")
    (install_dir / ".env").write_text(
        "MARKETMIND_IMAGE=ghcr.io/example/marketmind@sha256:old\n",
        encoding="utf-8",
    )
    postgres_image = "pgvector/pgvector@sha256:" + "b" * 64
    (install_dir / "release.env").write_text(
        "MARKETMIND_IMAGE=ghcr.io/example/marketmind@sha256:old\n"
        f"MARKETMIND_POSTGRES_IMAGE={postgres_image}\n"
        "RELEASE_TAG=v1.0.0\n",
        encoding="utf-8",
    )
    for filename in (".env.example", "docker-compose.prod.yml", "docker-compose.postgres.yml"):
        (install_dir / filename).write_text("old\n", encoding="utf-8")

    image_root = tmp_path / "image"
    (image_root / "deploy").mkdir(parents=True)
    (image_root / ".env.example").write_text("new example\n", encoding="utf-8")
    for filename in ("docker-compose.prod.yml", "docker-compose.postgres.yml"):
        (image_root / filename).write_text("services: {}\n", encoding="utf-8")
    shutil.copy(ROOT / "deploy/update.sh", image_root / "deploy/update.sh")
    shutil.copy(ROOT / "deploy/marketmind.sh", image_root / "deploy/marketmind.sh")
    (image_root / "deploy/install.sh").write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" > state/systemd-refresh\n',
        encoding="utf-8",
    )
    (image_root / "deploy/dashboard.sh").write_text(
        "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
    )
    for script in (image_root / "deploy").iterdir():
        script.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker-calls"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$DOCKER_LOG"
if [[ "${1:-}" == image && "${2:-}" == inspect ]]; then
  if [[ "$*" == *org.opencontainers.image.version* ]]; then
    printf 'v1.1.0\\n'
  else
    printf 'ghcr.io/example/marketmind@sha256:%064d\\n' 0
  fi
elif [[ "${1:-}" == create ]]; then
  printf 'temporary-container\\n'
elif [[ "${1:-}" == cp ]]; then
  source_path=${2#*:/app/}
  cp -a "$FAKE_IMAGE_ROOT/$source_path" "$3"
fi
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    fake_curl = fake_bin / "curl"
    fake_curl.write_text("#!/usr/bin/env bash\nprintf '401'\n", encoding="utf-8")
    fake_curl.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DOCKER_LOG": str(docker_log),
        "FAKE_IMAGE_ROOT": str(image_root),
    }

    subprocess.run(
        ["bash", str(deploy_dir / "update.sh"), "latest"],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )

    expected_image = "ghcr.io/example/marketmind@sha256:" + "0" * 64
    assert f"MARKETMIND_IMAGE={expected_image}" in (install_dir / ".env").read_text(
        encoding="utf-8"
    )
    assert (install_dir / "state/current-release").read_text(encoding="utf-8") == "v1.1.0\n"
    calls = docker_log.read_text(encoding="utf-8")
    assert "pull ghcr.io/example/marketmind:latest" in calls
    assert "create --entrypoint /bin/true" in calls
    assert "cp temporary-container:/app/deploy" in calls


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
    assert "marketmind dashboard tailscale enable" in runbook
    assert "Tailscale Funnel" in runbook
    assert "./deploy/dashboard.sh enable" in runbook
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
    assert "marketmind update" in runbook
    assert "marketmind update vX.Y.Z" in runbook
    assert "./deploy/update.sh marketmind-vX.Y.Z-deploy.tar.gz" in runbook
    assert "mcp_servers:" in runbook
    assert 'Authorization: "Bearer ${MARKETMIND_MCP_TOKEN}"' in runbook
    assert "hermes mcp test marketmind" in runbook
