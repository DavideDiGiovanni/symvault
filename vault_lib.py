"""Vault library — constants, DB, filesystem, ignore helpers, utilities."""

import fcntl
import fnmatch
import glob as globmod
import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import click

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "0.2.0"
VAULT_DIR = ".vault"
OBJECTS_DIR = os.path.join(VAULT_DIR, "objects")
DB_PATH = os.path.join(VAULT_DIR, "vault.db")
LOCK_PATH = os.path.join(VAULT_DIR, "lock")
MIN_SIZE = 1024  # 1KB

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    hash       TEXT PRIMARY KEY,
    vault_path TEXT NOT NULL,
    extension  TEXT,
    size       INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    blob_mtime REAL,
    blob_size  INTEGER
);
CREATE TABLE IF NOT EXISTS links (
    original_path TEXT PRIMARY KEY,
    hash          TEXT NOT NULL REFERENCES files(hash),
    created       TEXT NOT NULL,
    mtime         REAL NOT NULL,
    size          INTEGER NOT NULL,
    inode         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);
"""

MIGRATIONS = [
    "ALTER TABLE files ADD COLUMN blob_mtime REAL",
    "ALTER TABLE files ADD COLUMN blob_size INTEGER",
]

DEFAULT_VAULTIGNORE = (
    "# Patterns to exclude from vault (one per line, fnmatch syntax)\n"
    ".vault\n.vaultignore\n*.tmp\n*.log\n"
)


def _migrate_db(db):
    """Apply schema migrations for existing databases."""
    for sql in MIGRATIONS:
        try:
            db.execute(sql)
        except sqlite3.OperationalError:
            pass


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def find_vault_root(start="."):
    """Walk up from start to find the nearest .vault directory."""
    p = Path(start).resolve()
    while True:
        if (p / VAULT_DIR).is_dir():
            return p
        if p.parent == p:
            return None
        p = p.parent


def get_db(root):
    db = sqlite3.connect(str(root / DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA foreign_keys=ON")
    _migrate_db(db)
    return db


def acquire_lock(root):
    """Acquire an exclusive file lock. Returns the lock fd or exits."""
    lock_file = root / LOCK_PATH
    fd = open(lock_file, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        return fd
    except OSError:
        fd.close()
        click.echo(click.style("Another vault operation is running. Aborting.", fg="red"), err=True)
        raise SystemExit(1)


def release_lock(fd):
    """Release the file lock."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def sha256_file(path, buf_size=1048576):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(buf_size):
            h.update(chunk)
    return h.hexdigest()


def vault_blob_path(root, file_hash, ext):
    """Git-style sharding: ab/cdef1234...ext"""
    return root / OBJECTS_DIR / file_hash[:2] / (file_hash[2:] + ext)


def make_vault_symlink(blob: Path, link_path: Path):
    """Create a symlink at *link_path* pointing to *blob* using a relative path.

    The relative target is computed from the symlink's parent directory so that
    the link stays valid as long as the vault root is moved as a whole.
    """
    rel_target = os.path.relpath(blob.resolve(), link_path.resolve().parent)
    os.symlink(rel_target, link_path)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Ignore patterns
# ---------------------------------------------------------------------------

def load_ignore_patterns(root):
    """Load glob patterns from .vaultignore, return compiled regexes."""
    ignore_file = root / ".vaultignore"
    if not ignore_file.exists():
        return []
    compiled = []
    for line in ignore_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            compiled.append(re.compile(fnmatch.translate(line)))
    return compiled


def is_ignored(rel_path, patterns):
    """Check if a relative path or any of its parent components matches any ignore pattern."""
    basename = os.path.basename(rel_path)
    if any(p.match(rel_path) or p.match(basename) for p in patterns):
        return True
    # Check each parent directory component so that a pattern like "cassaforte"
    # also ignores "cassaforte/sub/file.txt".
    parts = Path(rel_path).parts
    for i in range(len(parts) - 1):
        component = str(Path(*parts[: i + 1]))
        if any(p.match(component) or p.match(parts[i]) for p in patterns):
            return True
    return False


# ---------------------------------------------------------------------------
# Verify helpers
# ---------------------------------------------------------------------------

def is_vault_symlink_path(path: Path, vault_root: Path) -> bool:
    """Return True if path is a symlink pointing into .vault/objects/."""
    if not path.is_symlink():
        return False
    try:
        target = str(path.resolve())
    except OSError:
        return False
    return str((vault_root / OBJECTS_DIR).resolve()) in target


def hash_from_blob_path(blob_target, root):
    """Extract hash from a vault blob path like .vault/objects/ab/cdef1234...ext"""
    try:
        rel = os.path.relpath(Path(blob_target).resolve(), root / OBJECTS_DIR)
        parts = Path(rel).parts
        if len(parts) == 2:
            shard, name = parts
            return shard + os.path.splitext(name)[0]
    except (ValueError, IndexError):
        pass
    return None


def find_vault_symlinks(root, db_links):
    """Walk filesystem to find symlinks pointing into .vault/objects/.
    Uses os.scandir for cached stat results."""
    vault_objects = str((root / OBJECTS_DIR).resolve())
    found = {}

    walk_roots = {str(root)}
    for rel_path in db_links:
        parts = Path(rel_path).parts
        if parts and parts[0] == "..":
            non_dotdot = [i for i, x in enumerate(parts) if x != ".."]
            if non_dotdot:
                top = root / Path(*parts[:non_dotdot[0] + 1])
                walk_roots.add(str(top.resolve()))

    def _scan_dir(directory):
        try:
            entries = os.scandir(directory)
        except (PermissionError, OSError):
            return
        with entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    rel_dir = os.path.relpath(entry.path, root)
                    if VAULT_DIR not in Path(rel_dir).parts:
                        _scan_dir(entry.path)
                elif entry.is_symlink():
                    try:
                        target = str(Path(entry.path).resolve())
                    except OSError:
                        continue
                    if vault_objects in target:
                        rel = os.path.relpath(entry.path, root)
                        h = hash_from_blob_path(target, root)
                        if h:
                            found[rel] = h

    for wr in walk_roots:
        if Path(wr).exists():
            _scan_dir(wr)
    return found


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def is_glob(s):
    return any(c in s for c in ("*", "?", "["))


def expand_paths(patterns):
    """Expand a list of paths/glob patterns into resolved Path objects."""
    result = []
    for p in patterns:
        if is_glob(p):
            result.extend(Path(m).resolve() for m in globmod.glob(p, recursive=True))
        else:
            result.append(Path(p).absolute())
    return result
