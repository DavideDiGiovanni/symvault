#!/usr/bin/env python3
"""Vault CLI — content-addressable file deduplication with symlinks."""

import fnmatch
import os
import re
import signal
import shutil
from pathlib import Path

import click

from symvault_lib import (
    VERSION, VAULT_DIR, OBJECTS_DIR, SCHEMA, MIN_SIZE, DEFAULT_VAULTIGNORE,
    find_vault_root, get_db, acquire_lock, release_lock,
    sha256_file, vault_blob_path, make_vault_symlink, now_iso,
    load_ignore_patterns, is_ignored,
    find_vault_symlinks, hash_from_blob_path,
    human_size, is_glob, expand_paths,
    is_vault_symlink_path, collect_vault_symlinks,
)


@click.group(context_settings={"max_content_width": 120})
@click.version_option(VERSION, prog_name="symvault")
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
    ignore = root / ".symvaultignore"
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
                    if entry.name == ".symvaultignore":
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


@cli.command()
def dupes():
    """Show duplicate files (same content, multiple symlinks)."""
    root = find_vault_root()
    if not root:
        click.echo("No vault found.", err=True)
        raise SystemExit(1)
    db = get_db(root)
    rows = db.execute(
        "SELECT f.hash, f.size, GROUP_CONCAT(l.original_path, '\n') "
        "FROM files f JOIN links l ON f.hash = l.hash "
        "GROUP BY f.hash HAVING COUNT(*) > 1 ORDER BY f.size DESC"
    ).fetchall()
    db.close()
    if not rows:
        click.echo("No duplicates found.")
        return
    for h, size, paths in rows:
        click.echo(click.style(f"\n{h[:12]}… ({human_size(size)})", fg="yellow"))
        for p in paths.split("\n"):
            click.echo(f"  {p}")
    click.echo(f"\n{len(rows)} group(s), {sum(len(r[2].split(chr(10))) - 1 for r in rows)} duplicate(s).")


@cli.command()
@click.argument("paths", nargs=-1, type=click.Path())
@click.option("--dry-run", is_flag=True, help="Show what would be reverted.")
@click.option("-v", "--verbose", is_flag=True, help="Show each file being reverted.")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt.")
def revert(paths, dry_run, verbose, yes):
    """Restore original files from vault. Supports paths and glob patterns."""
    root = find_vault_root()
    if not root:
        click.echo("No vault found.", err=True)
        raise SystemExit(1)

    lock = acquire_lock(root) if not dry_run else None
    db = get_db(root)
    try:
        if paths:
            has_globs = any(is_glob(p) for p in paths)
            if has_globs:
                all_rows = db.execute(
                    "SELECT l.original_path, f.vault_path FROM links l JOIN files f ON l.hash = f.hash"
                ).fetchall()
                matched = []
                for p in paths:
                    if is_glob(p):
                        matched.extend((o, v) for o, v in all_rows if fnmatch.fnmatch(o, p) or fnmatch.fnmatch(os.path.basename(o), p))
                    else:
                        target = os.path.relpath(Path(p).absolute(), root)
                        matched.extend((o, v) for o, v in all_rows if o == target or o.startswith(target + os.sep))
                seen, rows = set(), []
                for o, v in matched:
                    if o not in seen:
                        seen.add(o)
                        rows.append((o, v))
            else:
                rows, seen = [], set()
                for p in paths:
                    target = os.path.relpath(Path(p).absolute(), root)
                    for r in db.execute(
                        "SELECT l.original_path, f.vault_path FROM links l JOIN files f ON l.hash = f.hash "
                        "WHERE l.original_path = ? OR l.original_path LIKE ?",
                        (target, target + os.sep + "%"),
                    ):
                        if r[0] not in seen:
                            seen.add(r[0])
                            rows.append(r)
        else:
            rows = db.execute(
                "SELECT l.original_path, f.vault_path FROM links l JOIN files f ON l.hash = f.hash"
            ).fetchall()

        if not rows:
            click.echo("Nothing to revert.")
            return
        if not paths and not dry_run and not yes:
            click.confirm(f"Revert ALL {len(rows)} file(s) from vault?", abort=True)

        count, errors = 0, 0
        for orig, vault_rel in rows:
            abs_orig, abs_blob = root / orig, root / vault_rel
            if dry_run:
                click.echo(f"[revert] {orig}")
                count += 1
                continue
            if not abs_blob.exists():
                click.echo(click.style(f"[error] blob missing: {vault_rel}", fg="red"), err=True)
                errors += 1
                continue
            tmp = abs_orig.parent / (abs_orig.name + ".symvault_tmp")
            try:
                shutil.copy2(str(abs_blob), str(tmp))
            except OSError as e:
                tmp.unlink(missing_ok=True)
                click.echo(click.style(f"[error] copy failed ({e}): {orig}", fg="red"), err=True)
                errors += 1
                continue
            try:
                if abs_orig.is_symlink() or abs_orig.exists():
                    abs_orig.unlink()
                tmp.rename(abs_orig)
            except OSError as e:
                tmp.unlink(missing_ok=True)
                click.echo(click.style(f"[error] rename failed ({e}): {orig}", fg="red"), err=True)
                errors += 1
                continue
            db.execute("DELETE FROM links WHERE original_path = ?", (orig,))
            if verbose:
                click.echo(f"  {orig} ← {vault_rel}")
            count += 1

        if not dry_run:
            for _, vp in db.execute(
                "SELECT hash, vault_path FROM files WHERE hash NOT IN (SELECT DISTINCT hash FROM links)"
            ):
                blob = root / vp
                if blob.exists():
                    blob.unlink()
                    try:
                        blob.parent.rmdir()
                    except OSError:
                        pass
            db.execute("DELETE FROM files WHERE hash NOT IN (SELECT DISTINCT hash FROM links)")
            db.commit()

        click.echo(f"Reverted {click.style(str(count), fg='green')} file(s).")
        if errors:
            click.echo(click.style(f"{errors} error(s) — run 'vault verify' to check.", fg="red"))
    finally:
        db.close()
        if lock:
            release_lock(lock)


@cli.command()
@click.argument("path", type=click.Path())
@click.option("--dry-run", is_flag=True, help="Show what would be deleted.")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def delete(path, dry_run, yes):
    """Hard delete: remove blob and all its symlinks."""
    root = find_vault_root()
    if not root:
        click.echo("No vault found.", err=True)
        raise SystemExit(1)

    lock = acquire_lock(root) if not dry_run else None
    db = get_db(root)
    try:
        target = os.path.relpath(Path(path).absolute(), root)
        row = db.execute("SELECT hash FROM links WHERE original_path = ?", (target,)).fetchone()
        if not row:
            click.echo(f"No vault entry for: {target}", err=True)
            raise SystemExit(1)

        file_hash = row[0]
        blob_row = db.execute("SELECT vault_path, size FROM files WHERE hash = ?", (file_hash,)).fetchone()
        all_links = db.execute("SELECT original_path FROM links WHERE hash = ?", (file_hash,)).fetchall()

        click.echo(click.style(f"Blob:  {blob_row[0]} ({human_size(blob_row[1])})", fg="red"))
        click.echo(f"Hash:  {file_hash}")
        click.echo(click.style(f"Symlinks ({len(all_links)}):", fg="red"))
        for (orig,) in all_links:
            click.echo(f"  {orig}")

        if dry_run:
            return
        if not yes:
            click.confirm("Permanently delete this file and all its symlinks?", abort=True)

        for (orig,) in all_links:
            p = root / orig
            if p.is_symlink():
                p.unlink()

        blob_path = root / blob_row[0]
        if blob_path.exists():
            blob_path.unlink()
            try:
                blob_path.parent.rmdir()
            except OSError:
                pass

        db.execute("DELETE FROM links WHERE hash = ?", (file_hash,))
        db.execute("DELETE FROM files WHERE hash = ?", (file_hash,))
        db.commit()
        click.echo(click.style(f"Deleted blob + {len(all_links)} symlink(s).", fg="red"))
    finally:
        db.close()
        if lock:
            release_lock(lock)


@cli.command()
@click.option("--fix", is_flag=True, help="Auto-correct detected issues.")
def verify(fix):
    """Verify vault integrity and detect changes."""
    root = find_vault_root()
    if not root:
        click.echo("No vault found.", err=True)
        raise SystemExit(1)

    lock = acquire_lock(root)
    db = get_db(root)

    db_links = {r[0]: r[1] for r in db.execute("SELECT original_path, hash FROM links")}
    disk_links = find_vault_symlinks(root, db_links)

    missing, untracked = {}, {}
    for path, h in db_links.items():
        if path not in disk_links:
            missing[path] = h
    for path, h in disk_links.items():
        if path not in db_links:
            untracked[path] = h

    # Detect renames
    renames, missing_by_hash = [], {}
    for path, h in missing.items():
        missing_by_hash.setdefault(h, []).append(path)
    matched_missing, matched_untracked = set(), set()
    for new_path, h in untracked.items():
        if h in missing_by_hash and missing_by_hash[h]:
            old_path = missing_by_hash[h].pop(0)
            renames.append((old_path, new_path, h))
            matched_missing.add(old_path)
            matched_untracked.add(new_path)

    deleted = {p: h for p, h in missing.items() if p not in matched_missing}
    new_untracked_raw = {p: h for p, h in untracked.items() if p not in matched_untracked}
    known_hashes = {r[0] for r in db.execute("SELECT hash FROM files")}
    new_untracked = {p: h for p, h in new_untracked_raw.items() if h in known_hashes}
    broken_symlinks = {p: h for p, h in new_untracked_raw.items() if h not in known_hashes}

    # Blob integrity
    corrupt, missing_blobs = [], []
    for file_hash, vault_path, blob_mtime, blob_size in db.execute(
        "SELECT hash, vault_path, blob_mtime, blob_size FROM files"
    ):
        blob = root / vault_path
        if not blob.exists():
            missing_blobs.append((file_hash, vault_path))
            continue
        st = blob.stat()
        if blob_mtime is not None and st.st_mtime == blob_mtime and st.st_size == blob_size:
            continue
        actual = sha256_file(blob)
        if actual != file_hash:
            corrupt.append((vault_path, file_hash, actual))
        else:
            db.execute("UPDATE files SET blob_mtime = ?, blob_size = ? WHERE hash = ?",
                       (st.st_mtime, st.st_size, file_hash))

    # Orphan blobs on disk
    orphan_blobs = []
    objects_dir = root / OBJECTS_DIR
    if objects_dir.exists():
        known_vp = {r[0] for r in db.execute("SELECT vault_path FROM files")}
        for shard in objects_dir.iterdir():
            if not shard.is_dir():
                continue
            for blob in shard.iterdir():
                rel = str(blob.relative_to(root))
                if rel not in known_vp:
                    orphan_blobs.append(rel)

    unreferenced = db.execute(
        "SELECT hash, vault_path FROM files WHERE hash NOT IN (SELECT DISTINCT hash FROM links)"
    ).fetchall()

    # Output
    total_issues = 0
    sections = [
        ("Renamed:", "yellow", [(f"  {o} → {n}",) for o, n, _ in renames]),
        ("Deleted (symlink removed from disk):", "red", [(f"  {p}",) for p in deleted]),
        ("Untracked (symlink on disk, not in DB):", "cyan", [(f"  {p}",) for p in new_untracked]),
        ("Broken (symlink to missing blob, not recoverable):", "red", [(f"  {p}",) for p in broken_symlinks]),
        ("Corrupt (hash mismatch):", "red", [(f"  {vp} (expected {e[:12]}… got {a[:12]}…)",) for vp, e, a in corrupt]),
        ("Missing blobs:", "red", [(f"  {vp}",) for _, vp in missing_blobs]),
        ("Orphan blobs (on disk, not in DB):", "magenta", [(f"  {p}",) for p in orphan_blobs]),
        ("Unreferenced blobs (in DB, no symlinks):", "magenta", [(f"  {vp}",) for _, vp in unreferenced]),
    ]
    for title, color, items in sections:
        if items:
            bold = color == "red" and title not in ("Renamed:", "Deleted (symlink removed from disk):")
            click.echo(click.style(title, fg=color, bold=bold))
            for (line,) in items:
                click.echo(line)
            click.echo()
            total_issues += len(items)

    if total_issues == 0:
        click.echo(click.style("All good. No issues found.", fg="green"))
        db.commit()
        db.close()
        release_lock(lock)
        return

    click.echo(f"{total_issues} issue(s) found.")
    if not fix:
        click.echo('Run with --fix to auto-correct.')
        db.commit()
        db.close()
        release_lock(lock)
        return

    # Fix
    fixed = 0
    for old, new, h in renames:
        db.execute("UPDATE links SET original_path = ? WHERE original_path = ?", (new, old))
        click.echo(click.style("  [fixed] ", fg="green") + f"renamed: {old} → {new}")
        fixed += 1
    for p in deleted:
        db.execute("DELETE FROM links WHERE original_path = ?", (p,))
        click.echo(click.style("  [fixed] ", fg="green") + f"removed link: {p}")
        fixed += 1
    for p, h in new_untracked.items():
        fpath = root / p
        try:
            st, lst = fpath.stat(), fpath.lstat()
            db.execute("INSERT OR REPLACE INTO links (original_path, hash, created, mtime, size, inode) VALUES (?, ?, ?, ?, ?, ?)",
                       (p, h, now_iso(), st.st_mtime, st.st_size, lst.st_ino))
            click.echo(click.style("  [fixed] ", fg="green") + f"tracked: {p}")
            fixed += 1
        except OSError:
            pass
    for p in broken_symlinks:
        fpath = root / p
        if fpath.is_symlink():
            fpath.unlink()
            click.echo(click.style("  [fixed] ", fg="green") + f"removed broken: {p}")
            fixed += 1
    for p in orphan_blobs:
        (root / p).unlink()
        try:
            (root / p).parent.rmdir()
        except OSError:
            pass
        click.echo(click.style("  [fixed] ", fg="green") + f"removed orphan: {p}")
        fixed += 1
    for h, vp in unreferenced:
        blob = root / vp
        if blob.exists():
            blob.unlink()
            try:
                blob.parent.rmdir()
            except OSError:
                pass
        db.execute("DELETE FROM files WHERE hash = ?", (h,))
        click.echo(click.style("  [fixed] ", fg="green") + f"removed unreferenced: {vp}")
        fixed += 1

    db.commit()
    db.close()
    release_lock(lock)
    click.echo(f"\nFixed {click.style(str(fixed), fg='green')} issue(s).")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would be cleaned without making changes.")
def gc(dry_run):
    """Garbage collect: remove orphan blobs, stale DB entries, and empty shard dirs."""
    root = find_vault_root()
    if not root:
        click.echo("No vault found.", err=True)
        raise SystemExit(1)

    lock = acquire_lock(root) if not dry_run else None
    db = get_db(root)
    cleaned = 0

    for (orig,) in db.execute("SELECT original_path FROM links"):
        if not (root / orig).exists():
            if dry_run:
                click.echo(click.style("[stale link] ", fg="red") + orig)
            else:
                db.execute("DELETE FROM links WHERE original_path = ?", (orig,))
            cleaned += 1

    for h, vp in db.execute(
        "SELECT hash, vault_path FROM files WHERE hash NOT IN (SELECT DISTINCT hash FROM links)"
    ):
        if dry_run:
            click.echo(click.style("[unreferenced] ", fg="magenta") + vp)
        else:
            blob = root / vp
            if blob.exists():
                blob.unlink()
            db.execute("DELETE FROM files WHERE hash = ?", (h,))
        cleaned += 1

    objects_dir = root / OBJECTS_DIR
    if objects_dir.exists():
        known = {r[0] for r in db.execute("SELECT vault_path FROM files")}
        for shard in objects_dir.iterdir():
            if not shard.is_dir():
                continue
            for blob in shard.iterdir():
                rel = str(blob.relative_to(root))
                if rel not in known:
                    if dry_run:
                        click.echo(click.style("[orphan blob] ", fg="magenta") + rel)
                    else:
                        blob.unlink()
                    cleaned += 1

    if not dry_run:
        if objects_dir.exists():
            for shard in objects_dir.iterdir():
                if shard.is_dir():
                    try:
                        shard.rmdir()
                    except OSError:
                        pass
        db.commit()
    db.close()
    if lock:
        release_lock(lock)

    if cleaned:
        verb = "would be cleaned" if dry_run else "cleaned"
        click.echo(f"{click.style(str(cleaned), fg='green')} item(s) {verb}.")
    else:
        click.echo(click.style("Nothing to clean.", fg="green"))


@cli.command()
@click.argument("dest", required=False, default=None, type=click.Path())
@click.option("--dry-run", is_flag=True, help="Show what would be created.")
@click.option("-v", "--verbose", is_flag=True, help="Show each symlink being created.")
def rebuild(dest, dry_run, verbose):
    """Recreate symlinks from DB. Without DEST, restore in original paths. With DEST, rebuild there and update DB."""
    root = find_vault_root()
    if not root:
        click.echo("No vault found.", err=True)
        raise SystemExit(1)

    relocate = dest is not None
    lock = acquire_lock(root) if not dry_run else None
    db = get_db(root)
    rows = db.execute(
        "SELECT l.original_path, f.vault_path, l.hash FROM links l JOIN files f ON l.hash = f.hash"
    ).fetchall()

    if not rows:
        click.echo("Nothing to rebuild (no links in DB).")
        db.close()
        return

    dest = Path(dest).resolve() if relocate else root
    count = 0
    for orig, vault_rel, file_hash in rows:
        blob = root / vault_rel
        if relocate:
            parts = Path(orig).parts
            clean = Path(*[p for p in parts if p != ".."]) if any(p == ".." for p in parts) else Path(orig)
            target_path = dest / clean
            new_rel = os.path.relpath(target_path, root)
        else:
            target_path = root / orig
            new_rel = None

        if dry_run:
            click.echo(f"[link] {target_path.relative_to(dest)}")
            count += 1
            continue

        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists() or target_path.is_symlink():
            target_path.unlink()
        make_vault_symlink(blob, target_path)
        if verbose:
            click.echo(f"  {os.path.relpath(target_path, dest)} → {vault_rel}")
        if relocate and new_rel != orig:
            st, lst = target_path.stat(), target_path.lstat()
            db.execute("DELETE FROM links WHERE original_path = ?", (orig,))
            db.execute(
                "INSERT OR REPLACE INTO links (original_path, hash, created, mtime, size, inode) VALUES (?, ?, ?, ?, ?, ?)",
                (new_rel, file_hash, now_iso(), st.st_mtime, st.st_size, lst.st_ino),
            )
        count += 1

    if relocate and not dry_run:
        db.commit()
    db.close()
    if lock:
        release_lock(lock)
    click.echo(f"Rebuilt {click.style(str(count), fg='green')} symlink(s) in {dest}")


@cli.command("list")
@click.argument("hash_prefix")
def list_cmd(hash_prefix):
    """Show all symlinks pointing to a blob (by hash or prefix)."""
    root = find_vault_root()
    if not root:
        click.echo("No vault found.", err=True)
        raise SystemExit(1)

    db = get_db(root)
    row = db.execute("SELECT hash, vault_path, size FROM files WHERE hash LIKE ?", (hash_prefix + "%",)).fetchone()
    if not row:
        click.echo(f"No blob matching: {hash_prefix}", err=True)
        db.close()
        raise SystemExit(1)

    file_hash, vault_path, size = row
    links = db.execute("SELECT original_path FROM links WHERE hash = ?", (file_hash,)).fetchall()
    db.close()

    click.echo(click.style(f"{file_hash[:12]}… ({human_size(size)})", fg="yellow"))
    click.echo(f"  blob: {vault_path}")
    if links:
        click.echo(f"  symlinks ({len(links)}):")
        for (p,) in links:
            click.echo(f"    {p}")
    else:
        click.echo(click.style("  no symlinks (unreferenced)", fg="magenta"))


@cli.command()
@click.argument("args", nargs=-1)
@click.option("--dry-run", is_flag=True, help="Show what would be copied without making changes.")
@click.option("--no-overwrite", is_flag=True, help="Skip files that already exist in destination.")
@click.option("-v", "--verbose", is_flag=True, help="Show each file being copied.")
def cp(args, dry_run, no_overwrite, verbose):
    """Copy vault files to a destination, restoring original names."""
    if len(args) < 2:
        click.echo("Usage: vault cp [OPTIONS] SOURCES... DEST", err=True)
        raise SystemExit(1)

    sources = args[:-1]
    dest = Path(args[-1])

    root = find_vault_root()
    if not root:
        click.echo("No vault found. Run 'vault init' first.", err=True)
        raise SystemExit(1)

    # Collect all symlinks from sources
    all_items: list[tuple[Path, Path, Path]] = []  # (symlink, blob, rel_dest)

    for src in sources:
        src_path = Path(src)

        if is_glob(src):
            found = collect_vault_symlinks(src_path, root)
            if not found:
                click.echo(click.style(f"[warning] no vault files match: {src}", fg="yellow"), err=True)
            all_items.extend(found)
        elif src_path.absolute().is_dir():
            found = collect_vault_symlinks(src_path, root)
            if not found:
                click.echo(click.style(f"[warning] no vault files in: {src}", fg="yellow"), err=True)
            all_items.extend(found)
        else:
            abs_src = src_path.absolute()
            if not is_vault_symlink_path(abs_src, root):
                click.echo(click.style(f"[error] not a vault symlink: {src}", fg="red"), err=True)
                continue
            blob = abs_src.resolve()
            all_items.append((abs_src, blob, Path(abs_src.name)))

    stats = {"copied": 0, "skipped": 0, "errors": 0, "total_bytes": 0}

    for symlink_path, blob_path, rel_path in all_items:
        dest_path = dest / rel_path

        if not blob_path.exists():
            click.echo(click.style(f"[error] blob missing: {blob_path}", fg="red"), err=True)
            stats["errors"] += 1
            continue

        if dry_run:
            click.echo(f"[copy] {symlink_path} → {dest_path}")
            stats["copied"] += 1
            stats["total_bytes"] += blob_path.stat().st_size
            continue

        if no_overwrite and dest_path.exists():
            click.echo(click.style(f"[skip] already exists: {dest_path}", fg="blue"), err=True)
            stats["skipped"] += 1
            continue

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(str(blob_path), str(dest_path))
        except OSError as e:
            dest_path.unlink(missing_ok=True)
            click.echo(click.style(f"[error] copy failed ({e}): {symlink_path}", fg="red"), err=True)
            stats["errors"] += 1
            continue

        file_size = dest_path.stat().st_size
        stats["copied"] += 1
        stats["total_bytes"] += file_size
        if verbose:
            click.echo(f"  {symlink_path} → {dest_path}")

    # Summary
    parts = [f"Copied {stats['copied']} file(s)"]
    parts.append(f"{stats['skipped']} skipped")
    if stats["errors"]:
        parts.append(click.style(f"{stats['errors']} error(s)", fg="red"))
    else:
        parts.append(f"{stats['errors']} error(s)")
    size_str = human_size(stats["total_bytes"])
    click.echo(f"{', '.join(parts)} ({size_str} total)")


if __name__ == "__main__":
    cli()
