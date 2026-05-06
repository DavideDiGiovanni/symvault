#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYMVAULT_PY="$SCRIPT_DIR/symvault.py"
LINK="/usr/local/bin/symvault"

if [ ! -f "$SYMVAULT_PY" ]; then
    echo "Error: symvault.py not found in $SCRIPT_DIR"
    exit 1
fi

chmod +x "$SYMVAULT_PY"
sudo ln -sf "$SYMVAULT_PY" "$LINK"

# Detect shell and add completion
SHELL_NAME="$(basename "$SHELL")"
case "$SHELL_NAME" in
    zsh)
        RC="$HOME/.zshrc"
        LINE='eval "$(_SYMVAULT_COMPLETE=zsh_source symvault)"'
        ;;
    bash)
        RC="$HOME/.bashrc"
        LINE='eval "$(_SYMVAULT_COMPLETE=bash_source symvault)"'
        ;;
    *)
        echo "Installed: $LINK → $SYMVAULT_PY"
        echo "Tab completion not supported for $SHELL_NAME (only zsh/bash)."
        exit 0
        ;;
esac

if ! grep -qF "$LINE" "$RC" 2>/dev/null; then
    echo "" >> "$RC"
    echo "# Symvault CLI tab completion" >> "$RC"
    echo "$LINE" >> "$RC"
    echo "Installed: $LINK → $SYMVAULT_PY"
    echo "Tab completion added to $RC"
    echo "Run: source $RC"
else
    echo "Installed: $LINK → $SYMVAULT_PY"
    echo "Tab completion already configured in $RC"
fi
