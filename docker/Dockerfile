# Claude CLI Worker Container
FROM ubuntu:22.04

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    ca-certificates \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install Claude CLI
RUN npm install -g @anthropic-ai/claude-code

# Add claude to path
ENV PATH="/root/.claude/bin:$PATH"

# Git config for commits
RUN git config --global user.email "worker@svengalibot.local" && \
    git config --global user.name "Svengalibot Worker"

# Working directory
WORKDIR /workspace

# Default command
CMD ["claude", "--version"]
