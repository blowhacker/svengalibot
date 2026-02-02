#!/bin/bash
# Build the svengalibot-worker Docker image

set -e

cd "$(dirname "$0")"

echo "Building svengalibot-worker Docker image..."
docker build -t svengalibot-worker .

echo "Done! Test with:"
echo "  docker run --rm svengalibot-worker claude --version"
