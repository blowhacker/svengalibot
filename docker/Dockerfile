# Claude CLI Worker Container
FROM ubuntu:22.04

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    ca-certificates \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20 (required for Claude CLI)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install Claude CLI globally (as root)
RUN npm install -g @anthropic-ai/claude-code

# Create non-root user (Claude CLI won't run --dangerously-skip-permissions as root)
RUN useradd -m -s /bin/bash worker && \
    echo "worker ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Git config for worker user
RUN su - worker -c 'git config --global user.email "worker@svengalibot.local" && git config --global user.name "Svengalibot Worker"'

# Create .claude directory for worker user (auth files will be copied at runtime)
RUN mkdir -p /home/worker/.claude && \
    chown -R worker:worker /home/worker/.claude

# Switch to worker user
USER worker

# Working directory
WORKDIR /workspace

# Default command
CMD ["claude", "--version"]
