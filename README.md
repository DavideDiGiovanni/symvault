# Symvault – Content-Addressable File Deduplication

[![CI](https://github.com/DavideDiGiovanni/symvault/actions/workflows/ci.yml/badge.svg)](https://github.com/DavideDiGiovanni/symvault/actions/workflows/ci.yml)

Symvault is a Python CLI tool for content-addressable file deduplication. It replaces duplicate files with symlinks to a centralized blob store (`.symvault/objects/`), saving disk space while keeping the original folder structure fully navigable.

## How It Works

1. Files are hashed (SHA-256) and moved to `.symvault/objects/` with git-style sharding (`ab/cdef...ext`)
2. The original file is replaced by a symlink pointing to the blob
3. Duplicate files (same hash) share a single blob
4. All metadata is tracked in a SQLite database (`.symvault/vault.db`)
5. Rescans are optimized: mtime/size/inode are compared before recalculating the hash

## Installation

```bash
# Clone the repository
git clone https://github.com/DavideDiGiovanni/symvault.git
cd symvault

# Install via pip
pip install .

# Or via pipx (isolated)
pipx install .
```

For development:

```bash
pip install -e ".[dev]"
```

## Quick Start

```bash
# Initialize a vault in the current directory
symvault init

# Scan and deduplicate files
symvault scan .

# Check vault statistics
symvault status
```

## Commands

| Command | Description |
|---|---|
| `symvault init` | Initialize a new vault in the current directory |
| `symvault scan [path...]` | Scan recursively and deduplicate files (progress bar, glob support) |
| `symvault status` | Show statistics (unique files, duplicates, space saved) |
| `symvault dupes` | Show duplicate file groups (same content, multiple symlinks) |
| `symvault list <hash>` | Show all symlinks pointing to a blob (supports hash prefix) |
| `symvault revert [path...]` | Restore original files from vault (glob support) |
| `symvault delete <path>` | Permanently delete a file and all its symlinks |
| `symvault verify` | Check bidirectional integrity (DB ↔ disk) with rename detection |
| `symvault gc` | Remove orphan blobs, stale links, empty shard dirs |
| `symvault rebuild [dest]` | Recreate symlinks in-place or to a new destination |
| `symvault cp [source...] <dest>` | Copy vault files to a destination as real files (read-only) |

## Common Options

- `--dry-run` — show what would be done without making changes
- `-v` / `--verbose` — show detailed output
- `-y` / `--yes` — skip interactive confirmation
- `--fix` — auto-correct issues detected by `verify`
- `--exclude` — glob pattern(s) to exclude from scan (repeatable)
- `--no-overwrite` — skip existing files in destination (`cp` only)

## .symvaultignore

A file in the vault root with glob patterns (one per line) to exclude files from scanning. Created automatically by `init` with sensible defaults.

```
# Comments with #
*.tmp
*.log
SomeFolder/*
```

Matching is done on both the full relative path and the basename. `.symvaultignore` itself is always excluded.

## Platform Support

- **Linux / macOS**: primary target (uses `fcntl.flock` for file locking)
- **Windows**: separate module (`vault_win.py`) with `msvcrt` locking

## Running Tests

```bash
pip install -e ".[dev]"
pytest
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, branching model, and code style guidelines.

## 🇮🇹 Versione Italiana

La documentazione italiana è disponibile in [README_IT.md](README_IT.md).

## License

[MIT](LICENSE)
