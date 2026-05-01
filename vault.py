#!/usr/bin/env python3
"""Vault CLI — content-addressable file deduplication with symlinks."""

import os
import re
import signal
import shutil
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
    batch, COMMIT_EVERY, interrupted = 0, 100, False

    def _handle_sigint(sig, frame):
        nonlocal interrupted
        interrupted = True

    prev_handler = signal.signal(signal.SIGINT, _handle_sigint)
    skip_log = []

    try:
        with click.progressbar(candidates, label="Scanning", show_pos=True) as bar:
            for fpath, st in bar:
                if interrupted:
                    break
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

                file_hash = sha256_file(abs_path)
                ext = fpath.suffix

                if dry_run:
                    if file_hash in known_hashes:
                        click.echo(click.style("[dedup] ", fg="yellow") + rel_path)
                        stats["dedup"] += 1
                    else:
                        click.echo(click.style("[new] ", fg="green") + rel_path)
                        stats["new"] += 1
                    continue

                existing_path = known_hashes.get(file_hash)
                if existing_path:
                    blob = root / existing_path
                    cur_st = fpath.stat()
                    if cur_st.st_mtime != st.st_mtime or cur_st.st_size != st.st_size:
                        if verbose:
                            skip_log.append(f"changed during scan: {rel_path}")
                        stats["skipped"] += 1
                        continue
                    os.remove(abs_path)
                    make_vault_symlink(blob, abs_path)
                    stats["dedup"] += 1
                else:
                    blob = vault_blob_path(root, file_hash, ext)
                    blob.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(str(abs_path), str(blob))
                    except OSError as e:
                        blob.unlink(missing_ok=True)
                        click.echo(click.style(f"[error] copy failed ({e}): {rel_path}", fg="red"), err=True)
                        continue
                    if blob.stat().st_size != st.st_size:
                        blob.unlink()
                        click.echo(click.style(f"[error] copy size mismatch: {rel_path}", fg="red"), err=True)
                        continue
                    cur_st = fpath.stat()
                    if cur_st.st_mtime != st.st_mtime or cur_st.st_size != st.st_size:
                        blob.unlink()
                        if verbose:
                            skip_log.append(f"changed during scan: {rel_path}")
                        stats["skipped"] += 1
                        continue
                    os.remove(abs_path)
                    make_vault_symlink(blob, abs_path)
                    vault_rel = str(blob.relative_to(root))
                    blob_st = blob.stat()
                    db.execute(
                        "INSERT INTO files (hash, vault_path, extension, size, first_seen, blob_mtime, blob_size) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (file_hash, vault_rel, ext, st.st_size, now_iso(), blob_st.st_mtime, blob_st.st_size),
                    )
                    known_hashes[file_hash] = vault_rel
                    stats["new"] += 1

                new_st = fpath.lstat()
                target_stat = fpath.stat()
                db.execute(
                    "INSERT OR REPLACE INTO links (original_path, hash, created, mtime, size, inode) VALUES (?, ?, ?, ?, ?, ?)",
                    (rel_path, file_hash, now_iso(), target_stat.st_mtime, target_stat.st_size, new_st.st_ino),
                )
                batch += 1
                if batch % COMMIT_EVERY == 0:
                    db.commit()
    finally:
        signal.signal(signal.SIGINT, prev_handler)
        db.commit()
        db.close()
        if lock:
            release_lock(lock)

    if interrupted:
        click.echo(click.style("\nInterrupted. Partial work saved.", fg="yellow"))
    if skip_log:
        click.echo(click.style(f"\nSkipped ({len(skip_log)}):", fg="blue"))
        for msg in skip_log:
            click.echo(f"  {msg}")
    click.echo(
        f"Done: {click.style(str(stats['new']), fg='green')} new, "
        f"{click.style(str(stats['dedup']), fg='yellow')} deduplicated, "
        f"{stats['unchanged']} unchanged, {stats['skipped']} skipped"
    )


@cli.command()
@click.option("-v", "--verbose", is_flag=True, help="Show duplicate groups and orphan details.")
def status(verbose):
    """Show vault statistics."""
    root = find_vault_root()
    if not root:
        click.echo("No vault found.", err=True)
        raise SystemExit(1)

    db = get_db(root)
    blobs = db.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files").fetchone()
    links = db.execute("SELECT COUNT(*) FROM links").fetchone()
    duplicates = links[0] - blobs[0]
    saved = db.execute(
        "SELECT COALESCE(SUM(f.size), 0) FROM links l JOIN files f ON l.hash = f.hash"
    ).fetchone()[0]
    saved_dedup = saved - blobs[1] if saved > blobs[1] else 0

    click.echo(f"Unique files in vault: {blobs[0]} ({human_size(blobs[1])})")
    click.echo(f"Symlinks tracked:      {links[0]}")
    click.echo(f"Duplicates removed:    {duplicates}")
    click.echo(f"Space saved:           {click.style(human_size(saved_dedup), fg='green')}")

    if verbose:
        orphan_paths = [o for (o,) in db.execute("SELECT original_path FROM links") if not (root / o).exists()]
        orphan_str = click.style(str(len(orphan_paths)), fg="red") if orphan_paths else str(len(orphan_paths))
        click.echo(f"Orphan symlinks:       {orphan_str}")
        dupe_rows = db.execute(
            "SELECT f.hash, f.size, GROUP_CONCAT(l.original_path, '\n') "
            "FROM files f JOIN links l ON f.hash = l.hash "
            "GROUP BY f.hash HAVING COUNT(*) > 1 ORDER BY f.size DESC"
        ).fetchall()
        if dupe_rows:
            click.echo(click.style(f"\nDuplicate groups ({len(dupe_rows)}):", fg="yellow"))
            for h, size, paths in dupe_rows:
                click.echo(click.style(f"  {h[:12]}… ({human_size(size)})", fg="yellow"))
                for p in paths.split("\n"):
                    click.echo(f"    {p}")
        if orphan_paths:
            click.echo(click.style(f"\nOrphan symlinks ({len(orphan_paths)}):", fg="red"))
            for p in orphan_paths:
                click.echo(f"  {p}")
    else:
        click.echo("Orphan symlinks:       (use -v to check)")
    db.close()


if __name__ == "__main__":
    cli()
