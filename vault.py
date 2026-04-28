#!/usr/bin/env python3
"""Vault CLI — content-addressable file deduplication with symlinks."""

from pathlib import Path

import click

from vault_lib import (
    VERSION, VAULT_DIR, OBJECTS_DIR, SCHEMA, DEFAULT_VAULTIGNORE,
    get_db,
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


if __name__ == "__main__":
    cli()
