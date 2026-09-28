"""Contracts for the host-native release and independent PostgreSQL deployment."""

from __future__ import annotations

import hashlib
import os
import pty
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


def _text(path: str) -> str:
    return (ROOT / path).read_text()


def test_native_release_contains_application_and_verified_archive() -> None:
    workflow = _text(".github/workflows/release.yml")
    updater = _text("deploy/update.sh")
    assert (
        "git archive HEAD agent data server dashboard deploy .env.example pyproject.toml uv.lock"
        in workflow
    )
    assert "sha256sum" in workflow and "sha256sum -c" in updater
    assert "member.isfile() or member.isdir()" in updater
    assert "uv sync --frozen --no-dev" in updater
    assert "docker " not in updater.lower()
    assert "docker " not in _text("deploy/install.sh").lower()


def test_update_preserves_mutable_files_and_rolls_back_on_failed_readiness() -> None:
    updater = _text("deploy/update.sh")
    assert '"$install_dir/releases/current"' in updater
    assert '"$install_dir/state/refresh/deployment.lock"' in updater
    assert "previous=$(readlink" in updater
    assert "rollback()" in updater
    assert "status" in updater and "401" in updater
    assert '"$install_dir/secrets"' not in updater
    assert 'cp -a "$payload/." "$new_dir/"' in updater
    assert 'ln -s "$install_dir/data/raw"' in updater
    assert 'UV_PYTHON_INSTALL_DIR="$install_dir/runtime/python"' in updater
    assert "uv python install 3.12" in updater
    assert "uv sync --frozen --no-dev --managed-python --python 3.12" in updater
    assert '"$python_path" == "$UV_PYTHON_INSTALL_DIR/"*' in updater


def test_release_provides_location_independent_installer() -> None:
    workflow = _text(".github/workflows/release.yml")
    installer = _text("deploy/bootstrap.sh")
    assert "install -m 0755 deploy/bootstrap.sh" in workflow
    assert '"marketmind-${RELEASE_TAG}-install.sh"' in workflow
    assert 'archive=$(realpath -- "$1")' in installer
    assert '"$install_dir/deploy/install.sh"' in installer
    assert '"$archive" != "$install_dir/$name"' in installer


def test_bootstrap_accepts_archive_from_another_directory_and_preserves_state(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "source" / "marketmind"
    (payload / "deploy").mkdir(parents=True)
    installer = payload / "deploy" / "install.sh"
    installer.write_text(
        '#!/bin/sh\nprintf installed > "$(dirname "$(dirname "$0")")/install-result"\n'
    )
    installer.chmod(0o755)
    (payload / "deploy" / "update.sh").write_text("#!/bin/sh\n")
    (payload / "pyproject.toml").write_text("[project]\n")
    (payload / "uv.lock").write_text("version = 1\n")
    (payload / "release.env").write_text("RELEASE_TAG=v0.4.1\n")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    archive = downloads / "marketmind-v0.4.1-native.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname="marketmind")
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (downloads / f"{archive.name}.sha256").write_text(f"{checksum}  {archive.name}\n")

    target = tmp_path / "installed"
    (target / "secrets").mkdir(parents=True)
    (target / "secrets" / "keep").write_text("private")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sudo = fake_bin / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n')
    sudo.chmod(0o755)
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "MARKETMIND_INSTALL_DIR": str(target),
    }
    master, slave = pty.openpty()
    try:
        result = subprocess.run(
            ["bash", str(DEPLOY / "bootstrap.sh"), str(archive)],
            cwd=tmp_path,
            env=env,
            stdin=slave,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        os.close(master)
        os.close(slave)
    assert result.returncode == 0, result.stderr
    assert (target / "install-result").read_text() == "installed"
    assert (target / "secrets" / "keep").read_text() == "private"
    assert (target / archive.name).is_file()


@pytest.mark.parametrize("unsafe_member", [False, True])
def test_bootstrap_rejects_bad_checksum_or_unsafe_member(
    tmp_path: Path, unsafe_member: bool
) -> None:
    archive = tmp_path / "marketmind-v0.4.1-native.tar.gz"
    if unsafe_member:
        with tarfile.open(archive, "w:gz") as tar:
            member = tarfile.TarInfo("marketmind/deploy/install.sh")
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            tar.addfile(member)
    else:
        archive.write_bytes(b"invalid release")
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest() if unsafe_member else "0" * 64
    (tmp_path / f"{archive.name}.sha256").write_text(f"{checksum}  {archive.name}\n")
    target = tmp_path / "installed"
    env = os.environ | {"MARKETMIND_INSTALL_DIR": str(target)}
    master, slave = pty.openpty()
    try:
        result = subprocess.run(
            ["bash", str(DEPLOY / "bootstrap.sh"), str(archive)],
            cwd=tmp_path,
            env=env,
            stdin=slave,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        os.close(master)
        os.close(slave)
    assert result.returncode != 0
    assert not target.exists()


def test_installer_never_manages_an_existing_postgres_cluster() -> None:
    installer = _text("deploy/install.sh")
    assert "pg_lsclusters" in installer
    assert "postgresql-$major-pgvector" in installer
    assert "apt-cache depends postgresql" in installer
    assert "MARKETMIND_ADMIN_SERVICE" in installer
    assert "app_pgpass" in installer
    assert "systemctl enable --now postgresql" in installer
    assert "for name in daily universe profiles financials esef backup" in installer
    assert "systemctl enable --now marketmind-backup.timer" not in installer


def test_provisioning_uses_admin_connection_without_password_arguments() -> None:
    for name in ("provision-marketmind-db.sh", "provision-dashboard-db.sh"):
        script = _text(f"deploy/{name}")
        assert "MARKETMIND_ADMIN_SERVICE" in script
        assert "local-postgres" in script
        assert "sudo -n -u postgres psql" in script
        assert "PASSWORD %L" in script
        assert "docker " not in script.lower()
    assert "SELECT ON TABLE public.agent_runs" in _text("deploy/provision-dashboard-db.sh")


def test_native_services_bind_loopback_and_dashboard_credentials_are_scoped() -> None:
    assert 'os.getenv("MARKETMIND_MCP_HOST"' in _text("server/mcp.py")
    assert "MARKETMIND_MCP_HOST=127.0.0.1" in _text("deploy/native-run.sh")
    assert "127.0.0.1" in _text("deploy/native-run.sh")
    dashboard = _text("deploy/systemd/marketmind-dashboard.service")
    assert "User=marketmind-dashboard" in dashboard
    assert "LoadCredential=dashboard_pgpass:" in dashboard
    assert "LoadCredential=mcp_token:" in dashboard
    assert "llm_api_key" not in dashboard
    assert "app_pgpass" not in dashboard
    assert "InaccessiblePaths=" in dashboard
    assert "dashboard_pgpass" in _text("deploy/dashboard.sh")


def test_job_units_do_not_depend_on_docker() -> None:
    for unit in (DEPLOY / "systemd").glob("*.service"):
        assert "docker.service" not in unit.read_text()
    assert "docker " not in _text("deploy/run-job.sh").lower()
    assert "daily-refresh.lock" in _text("deploy/native-run.sh")


def test_backup_is_a_verified_set_and_restore_is_isolated() -> None:
    backup = _text("deploy/backup-set.sh")
    restore = _text("deploy/restore-check.sh")
    assert "pg_dump" in backup and "pg_restore" in backup
    assert "rclone check" in backup and "SHA256SUMS" in backup
    assert "pg_restore" in restore
    assert "docker " not in backup.lower() and "docker " not in restore.lower()


@pytest.mark.parametrize(
    "script",
    [
        "bootstrap.sh",
        "install.sh",
        "update.sh",
        "native-run.sh",
        "run-job.sh",
        "dashboard.sh",
        "provision-marketmind-db.sh",
        "provision-dashboard-db.sh",
        "backup-set.sh",
        "restore-check.sh",
    ],
)
def test_deployment_script_shell_syntax(script: str) -> None:
    subprocess.run(["bash", "-n", str(DEPLOY / script)], check=True)
