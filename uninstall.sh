#!/usr/bin/env bash
set -e

LINK="/usr/local/bin/vault"

if [ -L "$LINK" ]; then
    sudo rm "$LINK"
    echo "Removed: $LINK"
else
    echo "Nothing to remove: $LINK not found."
fi

# Remove completion from shell rc
SHELL_NAME="$(basename "$SHELL")"
case "$SHELL_NAME" in
    zsh)  RC="$HOME/.zshrc" ;;
    bash) RC="$HOME/.bashrc" ;;
    *)    exit 0 ;;
esac

if grep -qF "VAULT_COMPLETE" "$RC" 2>/dev/null; then
    sed -i '/# Vault CLI tab completion/d' "$RC"
    sed -i '/_VAULT_COMPLETE/d' "$RC"
    echo "Tab completion removed from $RC"
    echo "Run: source $RC"
else
    echo "No tab completion found in $RC"
fi
