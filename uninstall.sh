#!/usr/bin/env bash
set -e

LINK="/usr/local/bin/symvault"

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

if grep -qF "SYMVAULT_COMPLETE" "$RC" 2>/dev/null; then
    sed -i '/# Symvault CLI tab completion/d' "$RC"
    sed -i '/_SYMVAULT_COMPLETE/d' "$RC"
    echo "Tab completion removed from $RC"
    echo "Run: source $RC"
else
    echo "No tab completion found in $RC"
fi
