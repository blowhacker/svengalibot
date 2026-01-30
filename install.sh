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

# Detect architecture
detect_arch() {
    local arch=$(uname -m)
    case $arch in
        arm64|aarch64)
            echo "arm64"
            ;;
        x86_64|amd64)
            echo "x86_64"
            ;;
        *)
            echo "$arch"
            ;;
    esac
}

ARCH=$(detect_arch)

OS=$(detect_os)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo "╔═══════════════════════════════════════╗"
echo "║       Svengalibot Installer           ║"
echo "╚═══════════════════════════════════════╝"
echo ""

# Warn about ARM architecture
if [[ "$ARCH" == "arm64" ]]; then
    warn "Detected ARM64 architecture (Apple Silicon or ARM Linux)"
    warn "VirtualBox is not available - Docker will be used for sandboxing"
    echo ""
fi

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
            # Allow update to fail (some repos might be broken) but continue
            sudo apt-get update || warn "apt-get update had errors, continuing anyway..."
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

# Install Vagrant
install_vagrant() {
    if check_command vagrant; then
        return 0
    fi

    info "Installing Vagrant..."

    case $OS in
        debian)
            wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg 2>/dev/null || true
            echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
            sudo apt-get update || warn "apt-get update had errors, continuing..."
            sudo apt-get install -y vagrant
            ;;
        fedora)
            sudo dnf install -y vagrant
            ;;
        arch)
            sudo pacman -Sy --noconfirm vagrant
            ;;
        macos)
            brew install --cask vagrant
            ;;
        *)
            error "Please install Vagrant manually: https://developer.hashicorp.com/vagrant/downloads"
            ;;
    esac
    success "Vagrant installed"
}

# Install VirtualBox or Docker (depending on architecture)
install_virtualbox() {
    # On ARM Macs, VirtualBox doesn't work - use Docker instead
    if [[ "$OS" == "macos" && "$ARCH" == "arm64" ]]; then
        warn "VirtualBox is not supported on Apple Silicon (M1/M2/M3)"
        info "Using Docker as the VM provider instead..."
        install_docker
        return $?
    fi

    if check_command VBoxManage; then
        return 0
    fi

    info "Installing VirtualBox..."

    case $OS in
        debian)
            sudo apt-get install -y virtualbox
            ;;
        fedora)
            sudo dnf install -y VirtualBox
            ;;
        arch)
            sudo pacman -Sy --noconfirm virtualbox virtualbox-host-modules-arch
            ;;
        macos)
            brew install --cask virtualbox
            ;;
        *)
            warn "Please install VirtualBox manually: https://www.virtualbox.org/wiki/Downloads"
            ;;
    esac
    success "VirtualBox installed"
}

# Install Docker (alternative to VirtualBox, required for ARM)
install_docker() {
    if check_command docker; then
        return 0
    fi

    info "Installing Docker..."

    case $OS in
        debian)
            curl -fsSL https://get.docker.com | sudo bash
            sudo usermod -aG docker $USER
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

    npm install -g @anthropic-ai/claude-code
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
        warn "requirements.txt not found, creating minimal one..."
        cat > requirements.txt << 'EOF'
flask>=3.0.0
openai>=1.0.0
python-dotenv>=1.0.0
pyyaml>=6.0
requests>=2.31.0
EOF
        pip install -r requirements.txt
        success "Python dependencies installed"
    fi
}

# Create directory structure
setup_directories() {
    info "Creating directory structure..."

    cd "$SCRIPT_DIR"

    mkdir -p app
    mkdir -p templates
    mkdir -p static
    mkdir -p prompts/manager
    mkdir -p data/tasks
    mkdir -p data/repos
    mkdir -p vagrant

    success "Directory structure created"
}

# Create default config if it doesn't exist
setup_config() {
    info "Setting up configuration..."

    cd "$SCRIPT_DIR"

    # Create .env template
    if [[ ! -f ".env" ]]; then
        cat > .env << 'EOF'
# OpenAI API Key (required for manager)
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
  - tests should be deterministic

security:
  - never commit secrets
  - validate all external input
  - use parameterized queries
  - sanitize user-facing output

documentation:
  - add docstrings to public functions
  - keep comments minimal but meaningful
  - document non-obvious decisions

project_specific:
  # Add your own rules here
EOF
        success "Created default guide.yaml"
    fi

    # Create default config
    if [[ ! -f "data/config.yaml" ]]; then
        cat > data/config.yaml << 'EOF'
manager:
  provider: openai
  model: gpt-5.2
  # Alternative models: gpt-5.2, gpt-4-turbo

worker:
  type: claude-cli
  vm_pool_size: 3
  max_attempts_per_chunk: 3

vm:
  provider: vagrant
  base_box: ubuntu/jammy64
  memory: 4096
  cpus: 2

git:
  bare_repo_path: ./data/repos/workspace.git

ui:
  host: 127.0.0.1
  port: 5000
EOF
        success "Created default config.yaml"
    fi
}

# Setup Vagrant base box
setup_vagrant() {
    info "Setting up Vagrant configuration..."

    cd "$SCRIPT_DIR"

    if [[ ! -f "vagrant/Vagrantfile" ]]; then
        cat > vagrant/Vagrantfile << 'EOF'
# -*- mode: ruby -*-
# vi: set ft=ruby :

NUM_VMS = ENV['SVENGALI_VM_COUNT'] || 3

# Detect architecture for provider selection
def arm_architecture?
  host_arch = `uname -m`.strip
  ['arm64', 'aarch64'].include?(host_arch)
end

Vagrant.configure("2") do |config|
  # Use different box based on architecture
  if arm_architecture?
    # ARM64 - use Docker provider
    config.vm.provider "docker" do |d|
      d.image = "ubuntu:22.04"
      d.remains_running = true
      d.has_ssh = true
    end
  else
    # x86_64 - use VirtualBox
    config.vm.box = "ubuntu/jammy64"
  end

  (1..NUM_VMS.to_i).each do |i|
    config.vm.define "worker-#{i}" do |node|
      node.vm.hostname = "svengali-worker-#{i}"

      unless arm_architecture?
        node.vm.network "private_network", type: "dhcp"
      end

      node.vm.provider "virtualbox" do |vb|
        vb.memory = "4096"
        vb.cpus = 2
        vb.name = "svengali-worker-#{i}"
      end

      node.vm.provider "docker" do |d|
        d.image = "ubuntu:22.04"
        d.name = "svengali-worker-#{i}"
        d.remains_running = true
        d.has_ssh = true
        d.create_args = ["--memory=4g", "--cpus=2"]
      end

      node.vm.provision "shell", path: "provision.sh"
    end
  end
end
EOF
        success "Created Vagrantfile"
    fi

    if [[ ! -f "vagrant/provision.sh" ]]; then
        cat > vagrant/provision.sh << 'EOF'
#!/usr/bin/env bash
set -e

echo "Provisioning Svengalibot worker VM..."

# Update system
apt-get update
apt-get upgrade -y

# Install essentials
apt-get install -y \
    build-essential \
    git \
    curl \
    wget \
    vim \
    jq \
    unzip

# Install Python
apt-get install -y python3 python3-pip python3-venv

# Install Node.js (for Claude CLI)
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs

# Install Claude CLI
npm install -g @anthropic-ai/claude-code

# Install common dev tools
apt-get install -y \
    golang-go \
    rustc \
    cargo

# Install Docker (for containerized builds)
curl -fsSL https://get.docker.com | bash -

# Configure git
git config --global init.defaultBranch main

echo "Worker VM provisioned successfully!"
echo "NOTE: You'll need to authenticate Claude CLI manually:"
echo "  vagrant ssh worker-1"
echo "  claude auth login"
EOF
        chmod +x vagrant/provision.sh
        success "Created provision.sh"
    fi
}

# Initialize git bare repo for worker communication
setup_git_repo() {
    info "Setting up git bare repository..."

    cd "$SCRIPT_DIR"

    if [[ ! -d "data/repos/workspace.git" ]]; then
        git init --bare data/repos/workspace.git
        success "Created bare git repository"
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
    echo "2. Start the web UI:"
    echo -e "   ${YELLOW}source venv/bin/activate${NC}"
    echo -e "   ${YELLOW}python run.py${NC}"
    echo ""
    echo "3. (Optional) Start the VM pool:"
    echo -e "   ${YELLOW}cd vagrant && vagrant up${NC}"
    echo ""
    echo "4. (Optional) Authenticate Claude CLI in VMs:"
    echo -e "   ${YELLOW}vagrant ssh worker-1${NC}"
    echo -e "   ${YELLOW}claude auth login${NC}"
    echo ""
    echo -e "${BLUE}Web UI will be available at:${NC} http://127.0.0.1:5000"
    echo ""
}

# Main installation flow
main() {
    info "Detected OS: $OS ($ARCH)"
    echo ""

    # Check what's already installed
    info "Checking existing installations..."
    check_command python3 || true
    check_command git || true
    check_command vagrant || true
    check_command VBoxManage || true
    check_command claude || true
    check_command npm || true
    echo ""

    # Ask what to install
    read -p "Install system dependencies? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        install_system_deps
    fi

    read -p "Install Vagrant? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        install_vagrant
    fi

    read -p "Install VirtualBox? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        install_virtualbox
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
    setup_vagrant
    setup_git_repo

    print_summary
}

# Run with --yes flag to skip prompts
if [[ "$1" == "--yes" ]] || [[ "$1" == "-y" ]]; then
    install_system_deps
    install_vagrant
    install_virtualbox
    install_claude_cli
    setup_directories
    setup_python_env
    setup_config
    setup_vagrant
    setup_git_repo
    print_summary
else
    main
fi
