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

# Create non-root user (Claude CLI won't run --dangerously-skip-permissions as root)
RUN useradd -m -s /bin/bash worker && \
    echo "worker ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Switch to worker user
USER worker
WORKDIR /home/worker

# Install Claude CLI as worker user
RUN npm install -g @anthropic-ai/claude-code

# Git config for commits
RUN git config --global user.email "worker@svengalibot.local" && \
    git config --global user.name "Svengalibot Worker"

# Working directory
WORKDIR /workspace

# Default command
CMD ["claude", "--version"]
