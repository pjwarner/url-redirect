#!/usr/bin/env bash
set -eo pipefail

echo "=========================================="
echo "Starting url-redirect deployment update..."
echo "=========================================="

# Ensure script executes in the project directory regardless of where it's called from
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# Detect branch (main vs master)
BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "main")

echo "-> Fetching latest changes on branch: ${BRANCH}..."
git fetch origin "${BRANCH}"

echo "-> Resetting tracked files to origin/${BRANCH}..."
# Keeps untracked files like .env and ./data intact
git reset --hard "origin/${BRANCH}"

echo "-> Determining Docker Compose binary..."
if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
else
    echo "Error: Neither 'docker compose' nor 'docker-compose' was found." >&2
    exit 1
fi

echo "-> Rebuilding and restarting containers..."
# Rebuilds the app image and recreates containers with minimal downtime
${COMPOSE_CMD} up -d --build --remove-orphans

echo "-> Pruning dangling Docker images to preserve droplet disk..."
docker image prune -f

echo "=========================================="
echo "url-redirect update complete!"
echo "=========================================="