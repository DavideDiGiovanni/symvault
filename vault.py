#!/usr/bin/env python3
"""Vault CLI — content-addressable file deduplication with symlinks."""

import os
import re
from pathlib import Path

import click

from vault_lib import (
    VERSION, VAULT_DIR, OBJECTS_DIR, SCHEMA, MIN_SIZE, DEFAULT_VAULTIGNORE,
    find_vault_root, get_db, acquire_lock, release_lock,
    sha256_file, vault_blob_path, make_vault_symlink, now_iso,
    load_ignore_patterns, is_ignored,
    human_size, is_glob, expand_paths,
)


@click.group(context_settings={"max_content_width": 120})
@click.version_option(VERSION, prog_name="vault")
def cli():
    """Vault – content-addressable file deduplication."""
    pass


@cli.command()
def init():
    """Initialize a new vault in the current directory."""
    root = Path(".").resolve()
    vault = root / VAULT_DIR
    if vault.exists():
        click.echo("Vault already initialized.")
        return
    (root / OBJECTS_DIR).mkdir(parents=True)
    db = get_db(root)
    db.executescript(SCHEMA)
    db.close()
    ignore = root / ".vaultignore"
    if not ignore.exists():
        ignore.write_text(DEFAULT_VAULTIGNORE)
    click.echo(f"Vault initialized in {click.style(str(root), fg='green')}")


@cli.command()
@click.argument("paths", nargs=-1, type=click.Path())
@click.option("--dry-run", is_flag=True, help="Show what would be done without making changes.")
@click.option("-v", "--verbose", is_flag=True, help="Show skipped files and reasons.")
@click.option("--exclude", multiple=True, help="Glob pattern(s) to exclude (repeatable).")
def scan(paths, dry_run, verbose, exclude):
    """Scan PATH(s) recursively and deduplicate files. Supports glob patterns."""
    root = find_vault_root()
    if not root:
        click.echo("No vault found. Run 'vault init' first.", err=True)
        raise SystemExit(1)

    lock = acquire_lock(root) if not dry_run else None
    targets = expand_paths(paths) if paths else [Path(".").resolve()]
    db = get_db(root)
    ignore_patterns = load_ignore_patterns(root)
    if exclude:
        import fnmatch as _fm
        ignore_patterns.extend(re.compile(_fm.translate(e)) for e in exclude)

    # preload links and files for fast in-memory lookup
    links_cache = {}
    for r in db.execute("SELECT original_path, hash, mtime, size, inode FROM links"):
        links_cache[r[0]] = (r[1], r[2], r[3], r[4])
    known_hashes = {r[0]: r[1] for r in db.execute("SELECT hash, vault_path FROM files")}

    # collect candidates via scandir
    candidates = []
    def _collect(directory):
        try:
            entries = os.scandir(directory)
        except PermissionError:
            return
        with entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    rel_dir = os.path.relpath(Path(entry.path).resolve(), root)
                    if VAULT_DIR not in Path(rel_dir).parts and not is_ignored(rel_dir, ignore_patterns):
                        _collect(entry.path)
                elif entry.is_file(follow_symlinks=False) or entry.is_symlink():
                    if entry.name == ".vaultignore":
                        continue
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    candidates.append((Path(entry.path), st))

    for t in targets:
        if t.is_dir():
            _collect(t)
        elif t.is_file() or t.is_symlink():
            try:
                candidates.append((t, t.stat()))
            except OSError:
                continue
        else:
            click.echo(click.style(f"[warning] not found: {t}", fg="yellow"), err=True)

    stats = {"new": 0, "dedup": 0, "skipped": 0, "unchanged": 0}
    skip_log = []

    for fpath, st in candidates:
        abs_path = fpath.resolve()

        if fpath.is_symlink() and VAULT_DIR in str(fpath.resolve()):
            if verbose:
                skip_log.append(f"already in vault: {fpath}")
            stats["skipped"] += 1
            continue
        if st.st_size < MIN_SIZE:
            if verbose:
                skip_log.append(f"too small ({st.st_size}B): {fpath}")
            stats["skipped"] += 1
            continue

        rel_path = os.path.relpath(abs_path, root)
        if is_ignored(rel_path, ignore_patterns):
            if verbose:
                skip_log.append(f"ignored: {rel_path}")
            stats["skipped"] += 1
            continue

        cached = links_cache.get(rel_path)
        if cached and cached[1] == st.st_mtime and cached[2] == st.st_size and cached[3] == st.st_ino:
            stats["unchanged"] += 1
            continue

        # TODO: deduplication logic

    db.commit()
    db.close()
    if lock:
        release_lock(lock)

    if skip_log:
        click.echo(click.style(f"\nSkipped ({len(skip_log)}):", fg="blue"))
        for msg in skip_log:
            click.echo(f"  {msg}")
    click.echo(
        f"Done: {click.style(str(stats['new']), fg='green')} new, "
        f"{click.style(str(stats['dedup']), fg='yellow')} deduplicated, "
        f"{stats['unchanged']} unchanged, {stats['skipped']} skipped"
    )


if __name__ == "__main__":
    cli()
