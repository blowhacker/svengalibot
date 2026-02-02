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
            brew install python3 git curl
            ;;
        *)
            warn "Unknown OS. Please install manually: python3, pip, git, curl"
            ;;
    esac
}

# Install Docker
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
            brew install --cask docker
            warn "Please open Docker Desktop to complete installation"
            ;;
        *)
            warn "Please install Docker manually: https://docs.docker.com/get-docker/"
            return 1
            ;;
    esac
    success "Docker installed"
}

# Install Claude CLI
install_claude_cli() {
    if check_command claude; then
        success "Claude CLI already installed"
        return 0
    fi

    info "Installing Claude CLI..."

    # Claude CLI is installed via npm
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
                brew install node
                ;;
        esac
    fi

    npm install -g @anthropic-ai/claude-code 2>/dev/null || \
        warn "Claude CLI install via npm failed - install manually: https://claude.ai/download"
    success "Claude CLI installed"
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

    mkdir -p data/tasks
    mkdir -p data/repos
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
# OpenAI API Key (required for manager/reviewer)
OPENAI_API_KEY=your-api-key-here

# Flask config
FLASK_ENV=development
FLASK_DEBUG=1
EOF
        warn "Created .env file - please add your OPENAI_API_KEY"
    fi

    # Create default guide
    if [[ ! -f "data/guide.yaml" ]]; then
        cat > data/guide.yaml << 'EOF'
code_style:
  - prefer simple code over clever code
  - max function length: 50 lines
  - max file length: 500 lines
  - always handle errors explicitly
  - no magic numbers, use named constants
  - use descriptive variable names

testing:
  - every public function should be testable
  - prefer unit tests over integration tests
  - test edge cases explicitly

security:
  - never commit secrets
  - validate all external input
  - use parameterized queries

documentation:
  - add docstrings to public functions
  - keep comments minimal but meaningful

project_specific:
  # Add your own rules here
EOF
        success "Created default guide.yaml"
    fi

    # Create default config
    if [[ ! -f "data/config.yaml" ]]; then
        cat > data/config.yaml << 'EOF'
manager:
  model: gpt-4

worker:
  type: claude-cli
  mode: local  # local or docker
  max_attempts_per_chunk: 7

docker:
  memory: 4g
  cpus: 2
  timeout: 600

ui:
  host: 0.0.0.0
  port: 5000
EOF
        success "Created default config.yaml"
    fi
}

# Build Docker image
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
    echo "1. Add your OpenAI API key to .env:"
    echo -e "   ${YELLOW}echo 'OPENAI_API_KEY=sk-...' >> .env${NC}"
    echo ""
    echo "2. Authenticate Claude CLI:"
    echo -e "   ${YELLOW}claude auth login${NC}"
    echo ""
    echo "3. Start the web UI:"
    echo -e "   ${YELLOW}source venv/bin/activate${NC}"
    echo -e "   ${YELLOW}python run.py${NC}"
    echo ""
    echo "4. (Optional) Use Docker for isolated execution:"
    echo -e "   Edit data/config.yaml: set ${YELLOW}worker.mode: docker${NC}"
    echo ""
    echo -e "${BLUE}Web UI will be available at:${NC} http://localhost:5000"
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

    read -p "Install Docker? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        install_docker
    fi

    read -p "Install Claude CLI? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        install_claude_cli
    fi

    # Always do these
    setup_directories
    setup_python_env
    setup_config

    # Build Docker image if Docker is available
    if check_command docker; then
        read -p "Build Docker worker image? (y/N) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            build_docker_image
        fi
    fi

    print_summary
}

# Run with --yes flag to skip prompts
if [[ "$1" == "--yes" ]] || [[ "$1" == "-y" ]]; then
    install_system_deps
    install_docker
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
