#!/bin/bash
set -e

echo "⚡ Installing Kyrex..."

# Clone or update
if [ -d "$HOME/kyrex" ]; then
    echo "Updating existing install..."
    cd "$HOME/kyrex"
    git pull
else
    git clone https://github.com/kp84-hub/kyrex.git "$HOME/kyrex"
    cd "$HOME/kyrex"
fi

# Check dependencies
command -v go >/dev/null 2>&1 || { echo "Error: Go is not installed. Install from https://go.dev/dl/"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "Error: Python3 is not installed."; exit 1; }

# Python engine
echo "Installing Python engine..."
pip install -e kyrex_engine/ --break-system-packages --quiet

# Build binary
echo "Building kx binary..."
go build -o kx .

# Install globally
echo "Installing kx to /usr/local/bin..."
sudo cp kx /usr/local/bin/kx

echo ""
echo "✓ Kyrex installed successfully."
echo "  Run 'kx --setup' to configure your API provider."
echo "  Run 'kx' to launch the TUI."
echo "  Run 'kx -p \"prompt\"' for one-shot mode."
