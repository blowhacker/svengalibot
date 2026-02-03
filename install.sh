#!/usr/bin/env bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# Detect OS
detect_os() {
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if command -v apt-get &> /dev/null; then
            echo "debian"
        elif command -v dnf &> /dev/null; then
            echo "fedora"
        elif command -v pacman &> /dev/null; then
            echo "arch"
        else
            echo "linux-unknown"
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        echo "macos"
    else
        echo "unknown"
    fi
}

OS=$(detect_os)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# On Apple Silicon, ensure brew runs natively
BREW="brew"
if [[ "$OS" == "macos" ]] && [[ $(uname -m) == "arm64" || -d "/opt/homebrew" ]]; then
    BREW="arch -arm64 brew"
fi

echo ""
echo "╔═══════════════════════════════════════╗"
echo "║       Svengalibot Installer           ║"
echo "╚═══════════════════════════════════════╝"
echo ""

# Check for required commands
check_command() {
    if command -v "$1" &> /dev/null; then
        success "$1 found"
        return 0
    else
        return 1
    fi
}

# Install system dependencies
install_system_deps() {
    info "Installing system dependencies..."

    case $OS in
        debian)
            sudo apt-get update || warn "apt-get update had errors, continuing..."
            sudo apt-get install -y python3 python3-pip python3-venv git curl
            ;;
        fedora)
            sudo dnf install -y python3 python3-pip git curl
            ;;
        arch)
            sudo pacman -Sy --noconfirm python python-pip git curl
            ;;
        macos)
            if ! check_command brew; then
                error "Homebrew not found. Install from https://brew.sh"
            fi
            $BREW install python3 git curl
            ;;
        *)
            warn "Unknown OS. Please install manually: python3, pip, git, curl"
            ;;
    esac
}

# Install Docker (optional - for isolated execution)
install_docker() {
    if check_command docker; then
        success "Docker already installed"
        return 0
    fi

    info "Installing Docker..."

    case $OS in
        debian)
            curl -fsSL https://get.docker.com | sudo bash
            sudo usermod -aG docker $USER
            warn "You may need to log out and back in for Docker group to take effect"
            ;;
        fedora)
            sudo dnf install -y docker
            sudo systemctl enable --now docker
            sudo usermod -aG docker $USER
            ;;
        arch)
            sudo pacman -Sy --noconfirm docker
            sudo systemctl enable --now docker
            sudo usermod -aG docker $USER
            ;;
        macos)
            $BREW install --cask docker
            warn "Please open Docker Desktop to complete installation"
            ;;
        *)
            warn "Please install Docker manually: https://docs.docker.com/get-docker/"
            return 1
            ;;
    esac
    success "Docker installed"
}

# Install Claude Code CLI
install_claude_cli() {
    if check_command claude; then
        success "Claude Code already installed"
        return 0
    fi

    info "Installing Claude Code..."

    # Claude Code is installed via npm
    if ! check_command npm; then
        case $OS in
            debian)
                curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
                sudo apt-get install -y nodejs
                ;;
            fedora)
                sudo dnf install -y nodejs
                ;;
            arch)
                sudo pacman -Sy --noconfirm nodejs npm
                ;;
            macos)
                $BREW install node
                ;;
        esac
    fi

    npm install -g @anthropic-ai/claude-code 2>/dev/null || \
        warn "Claude Code install via npm failed - visit https://claude.ai/download"
    success "Claude Code installed"
}

# Setup Python virtual environment and dependencies
setup_python_env() {
    info "Setting up Python environment..."

    cd "$SCRIPT_DIR"

    # Create venv if it doesn't exist
    if [[ ! -d "venv" ]]; then
        python3 -m venv venv
        success "Created virtual environment"
    fi

    # Activate and install dependencies
    source venv/bin/activate
    pip install --upgrade pip

    if [[ -f "requirements.txt" ]]; then
        pip install -r requirements.txt
        success "Python dependencies installed"
    else
        warn "requirements.txt not found"
    fi
}

# Create directory structure
setup_directories() {
    info "Creating directory structure..."

    cd "$SCRIPT_DIR"

    mkdir -p data/projects

    success "Directory structure created"
}

# Create default config if it doesn't exist
setup_config() {
    info "Setting up configuration..."

    cd "$SCRIPT_DIR"

    # Create .env template
    if [[ ! -f ".env" ]]; then
        cat > .env << 'EOF'
# OpenAI API Key (optional - for collaborator mode)
OPENAI_API_KEY=your-api-key-here

# Flask config
FLASK_ENV=development
FLASK_DEBUG=1
EOF
        warn "Created .env file - add OPENAI_API_KEY if using collaborator mode"
    fi

    # Create default config
    if [[ ! -f "data/config.yaml" ]]; then
        cat > data/config.yaml << 'EOF'
manager:
  model: gpt-5.2

worker:
  mode: docker  # docker (recommended) or local

docker:
  memory: 4g
  cpus: 2

ui:
  host: 0.0.0.0
  port: 5011
EOF
        success "Created default config.yaml"
    fi
}

# Build Docker image (optional)
build_docker_image() {
    info "Building Docker worker image..."

    cd "$SCRIPT_DIR"

    if [[ -f "docker/Dockerfile" ]]; then
        docker build -t svengalibot-worker docker/
        success "Docker image built"
    else
        warn "docker/Dockerfile not found, skipping image build"
    fi
}

# Print final instructions
print_summary() {
    echo ""
    echo "╔═══════════════════════════════════════════════════════════╗"
    echo "║              Installation Complete!                       ║"
    echo "╚═══════════════════════════════════════════════════════════╝"
    echo ""
    echo -e "${GREEN}Next steps:${NC}"
    echo ""
    echo "1. Authenticate Claude Code (opens browser):"
    echo -e "   ${YELLOW}claude${NC}"
    echo ""
    echo "2. Start the web UI:"
    echo -e "   ${YELLOW}source venv/bin/activate${NC}"
    echo -e "   ${YELLOW}python run.py${NC}"
    echo ""
    echo "3. (Optional) Add OpenAI key for collaborator mode:"
    echo -e "   Edit .env or configure at ${YELLOW}http://localhost:5011/setup${NC}"
    echo ""
    echo -e "   ${YELLOW}Note:${NC} Docker mode is the default for safety (sandboxed execution)."
    echo -e "   If the Docker image wasn't built during install, run:"
    echo -e "   ${YELLOW}docker build -t svengalibot-worker docker/${NC}"
    echo ""
    echo -e "${BLUE}Web UI will be available at:${NC} http://localhost:5011"
    echo ""
}

# Main installation flow
main() {
    info "Detected OS: $OS"
    echo ""

    # Check what's already installed
    info "Checking existing installations..."
    check_command python3 || true
    check_command git || true
    check_command docker || true
    check_command claude || true
    check_command npm || true
    echo ""

    # Ask what to install
    read -p "Install system dependencies? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        install_system_deps
    fi

    read -p "Install Claude Code? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        install_claude_cli
    fi

    # Always do these
    setup_directories
    setup_python_env
    setup_config

    # Docker is optional
    if ! check_command docker; then
        read -p "Install Docker? (optional, for isolated execution) (y/N) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            install_docker
        fi
    fi

    # Build Docker image if Docker is available
    if check_command docker; then
        read -p "Build Docker worker image? (required for Docker mode) (Y/n) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            build_docker_image
        fi
    fi

    print_summary
}

# Run with --yes flag to skip prompts
if [[ "$1" == "--yes" ]] || [[ "$1" == "-y" ]]; then
    install_system_deps
    install_claude_cli
    setup_directories
    setup_python_env
    setup_config
    if check_command docker; then
        build_docker_image
    fi
    print_summary
else
    main
fi
