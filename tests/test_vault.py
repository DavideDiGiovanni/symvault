"""Test suite for vault CLI — init and scan commands."""

import os
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from vault import cli


@pytest.fixture
def vault_env(tmp_path, monkeypatch):
    """Set up a clean vault environment in tmp_path."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["init"])
    return tmp_path, runner


def _make_file(path, size_kb):
    """Create a file with random-ish content of given size in KB."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(os.urandom(size_kb * 1024))


def _is_vault_symlink(path):
    """Check if path is a symlink pointing into .vault/objects/."""
    p = Path(path)
    return p.is_symlink() and ".vault/objects/" in str(p.resolve())


# ── Test 1: Init + Scan base ──────────────────────────────────────────────

def test_init_scan_base(vault_env):
    root, runner = vault_env

    # verify init created the right structure
    assert (root / ".vault").is_dir()
    assert (root / ".vault" / "objects").is_dir()
    assert (root / ".vault" / "vault.db").is_file()
    assert (root / ".vaultignore").is_file()

    # create test files: matteo + copy (uscita03) + giulia
    _make_file(root / "Persone" / "matteo.jpg", 50)
    (root / "Vacanze 2026").mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "Persone" / "matteo.jpg", root / "Vacanze 2026" / "uscita03.jpg")
    _make_file(root / "Persone" / "giulia.jpg", 30)

    result = runner.invoke(cli, ["scan", "."])
    assert result.exit_code == 0
    assert "2 new" in result.output
    assert "1 deduplicated" in result.output

    # symlinks created
    assert _is_vault_symlink(root / "Persone" / "matteo.jpg")
    assert _is_vault_symlink(root / "Persone" / "giulia.jpg")
    assert _is_vault_symlink(root / "Vacanze 2026" / "uscita03.jpg")

    # matteo and uscita03 point to same blob
    assert (root / "Persone" / "matteo.jpg").resolve() == (root / "Vacanze 2026" / "uscita03.jpg").resolve()


# ── Test 2: Rescan (no changes) ──────────────────────────────────────────

def test_rescan_no_changes(vault_env):
    root, runner = vault_env
    _make_file(root / "a.jpg", 5)
    runner.invoke(cli, ["scan", "."])

    result = runner.invoke(cli, ["scan", "."])
    assert result.exit_code == 0
    assert "0 new" in result.output
    assert "0 deduplicated" in result.output


# ── Test 3: Rescan with new file ─────────────────────────────────────────

def test_rescan_new_file(vault_env):
    root, runner = vault_env
    _make_file(root / "a.jpg", 5)
    runner.invoke(cli, ["scan", "."])

    _make_file(root / "b.jpg", 10)
    result = runner.invoke(cli, ["scan", "."])
    assert result.exit_code == 0
    assert "1 new" in result.output


# ── Test 6: Dry-run ──────────────────────────────────────────────────────

def test_dry_run(vault_env):
    root, runner = vault_env
    _make_file(root / "nuovo.dat", 10)

    result = runner.invoke(cli, ["scan", "--dry-run", "."])
    assert result.exit_code == 0
    assert "[new]" in result.output

    # file should NOT be a symlink
    assert not (root / "nuovo.dat").is_symlink()
    assert (root / "nuovo.dat").is_file()


# ── Test 7: Verbose ──────────────────────────────────────────────────────

def test_verbose(vault_env):
    root, runner = vault_env
    _make_file(root / "a.jpg", 5)
    runner.invoke(cli, ["scan", "."])

    # rescan with verbose — symlinks should be reported as skipped
    result = runner.invoke(cli, ["scan", "-v", "."])
    assert result.exit_code == 0
    assert "already in vault" in result.output


# ── Test 13: .vaultignore ────────────────────────────────────────────────

def test_vaultignore(vault_env):
    root, runner = vault_env

    # add *.dat to ignore
    with open(root / ".vaultignore", "a") as f:
        f.write("\n*.dat\n")

    _make_file(root / "test.dat", 5)
    result = runner.invoke(cli, ["scan", "."])
    assert result.exit_code == 0

    # test.dat should NOT be a symlink
    assert not (root / "test.dat").is_symlink()


# ── Test 14: Files under 1KB ignored ─────────────────────────────────────

def test_small_file_ignored(vault_env):
    root, runner = vault_env
    (root / "piccolo.txt").write_text("tiny")

    result = runner.invoke(cli, ["scan", "."])
    assert result.exit_code == 0

    # should still be a real file
    assert (root / "piccolo.txt").is_file()
    assert not (root / "piccolo.txt").is_symlink()


# ── Test: --exclude ──────────────────────────────────────────────────────

def test_scan_exclude(vault_env):
    root, runner = vault_env
    _make_file(root / "photo.jpg", 10)
    _make_file(root / "video.mp4", 20)

    result = runner.invoke(cli, ["scan", "--exclude", "*.mp4", "."])
    assert result.exit_code == 0
    assert "1 new" in result.output

    # jpg should be symlink, mp4 should not
    assert _is_vault_symlink(root / "photo.jpg")
    assert not (root / "video.mp4").is_symlink()
