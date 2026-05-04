#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VAULT_PY="$SCRIPT_DIR/vault.py"
LINK="/usr/local/bin/vault"

if [ ! -f "$VAULT_PY" ]; then
    echo "Error: vault.py not found in $SCRIPT_DIR"
    exit 1
fi

chmod +x "$VAULT_PY"
sudo ln -sf "$VAULT_PY" "$LINK"

# Detect shell and add completion
SHELL_NAME="$(basename "$SHELL")"
case "$SHELL_NAME" in
    zsh)
        RC="$HOME/.zshrc"
        LINE='eval "$(_VAULT_COMPLETE=zsh_source vault)"'
        ;;
    bash)
        RC="$HOME/.bashrc"
        LINE='eval "$(_VAULT_COMPLETE=bash_source vault)"'
        ;;
    *)
        echo "Installed: $LINK → $VAULT_PY"
        echo "Tab completion not supported for $SHELL_NAME (only zsh/bash)."
        exit 0
        ;;
esac

if ! grep -qF "$LINE" "$RC" 2>/dev/null; then
    echo "" >> "$RC"
    echo "# Vault CLI tab completion" >> "$RC"
    echo "$LINE" >> "$RC"
    echo "Installed: $LINK → $VAULT_PY"
    echo "Tab completion added to $RC"
    echo "Run: source $RC"
else
    echo "Installed: $LINK → $VAULT_PY"
    echo "Tab completion already configured in $RC"
fi
