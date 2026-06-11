#!/bin/bash
set -e

echo "⚡ Installing Kyrex..."

# ── Detect OS ──────────────────────────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"

if [ "$OS" != "Linux" ]; then
    echo "Error: Kyrex installer currently supports Linux/WSL only."
    echo "  macOS/Windows native support coming soon."
    exit 1
fi

# ── Auto-install Python3 ───────────────────────────────────────────────────
if ! command -v python3 >/dev/null 2>&1; then
    echo "Installing Python3..."
    sudo apt-get update -qq
    sudo apt-get install -y python3 python3-pip
fi

if ! command -v pip3 >/dev/null 2>&1 && ! python3 -m pip --version >/dev/null 2>&1; then
    echo "Installing pip..."
    sudo apt-get install -y python3-pip
fi

# ── Auto-install Go ────────────────────────────────────────────────────────
if ! command -v go >/dev/null 2>&1; then
    echo "Installing Go 1.22.2..."
    GO_VERSION="1.22.2"

    if [ "$ARCH" = "x86_64" ]; then
        GO_ARCH="amd64"
    elif [ "$ARCH" = "aarch64" ]; then
        GO_ARCH="arm64"
    else
        echo "Error: Unsupported architecture: $ARCH"
        exit 1
    fi

    GO_TAR="go${GO_VERSION}.linux-${GO_ARCH}.tar.gz"
    echo "  Downloading $GO_TAR..."
    curl -fsSL "https://go.dev/dl/${GO_TAR}" -o "/tmp/${GO_TAR}"
    sudo rm -rf /usr/local/go
    sudo tar -C /usr/local -xzf "/tmp/${GO_TAR}"
    rm "/tmp/${GO_TAR}"

    export PATH=$PATH:/usr/local/go/bin

    # Persist to shell config
    SHELL_RC="$HOME/.bashrc"
    if [ -n "$ZSH_VERSION" ] || [ "$SHELL" = "/bin/zsh" ]; then
        SHELL_RC="$HOME/.zshrc"
    fi
    if ! grep -q '/usr/local/go/bin' "$SHELL_RC" 2>/dev/null; then
        echo 'export PATH=$PATH:/usr/local/go/bin' >> "$SHELL_RC"
    fi
    echo "  Go installed."
fi

# ── Clone or update repo ───────────────────────────────────────────────────
if [ -d "$HOME/kyrex/.git" ]; then
    echo "Updating existing install..."
    cd "$HOME/kyrex"
    git pull --quiet
else
    echo "Cloning Kyrex..."
    git clone --quiet https://github.com/kp84-hub/kyrex.git "$HOME/kyrex"
    cd "$HOME/kyrex"
fi

cd "$HOME/kyrex"

# ── Python engine ──────────────────────────────────────────────────────────
echo "Installing Python engine..."
python3 -m pip install -e kyrex_engine/ --break-system-packages --quiet 2>/dev/null || \
    pip3 install -e kyrex_engine/ --break-system-packages --quiet

# ── Build binary ───────────────────────────────────────────────────────────
echo "Building kx..."
/usr/local/go/bin/go build -o kx . 2>/dev/null || go build -o kx .

# ── Install globally ───────────────────────────────────────────────────────
mkdir -p "$HOME/.local/bin"
cp kx "$HOME/.local/bin/kx"
chmod +x "$HOME/.local/bin/kx"

# Also try /usr/local/bin for system-wide access
if sudo cp kx /usr/local/bin/kx 2>/dev/null; then
    :
fi

# Ensure ~/.local/bin is on PATH
SHELL_RC="$HOME/.bashrc"
if [ -n "$ZSH_VERSION" ] || [ "$SHELL" = "/bin/zsh" ]; then
    SHELL_RC="$HOME/.zshrc"
fi
if ! grep -q '$HOME/.local/bin' "$SHELL_RC" 2>/dev/null; then
    echo 'export PATH=$PATH:$HOME/.local/bin' >> "$SHELL_RC"
fi
export PATH=$PATH:$HOME/.local/bin

# ── Done ───────────────────────────────────────────────────────────────────
echo ""
echo "✅ Kyrex installed successfully."
echo ""
echo "  Run 'kx --setup' to configure your API key and model."
echo "  Run 'kx' to launch."
echo ""
echo "  If 'kx' is not found, run: source ~/.bashrc"
