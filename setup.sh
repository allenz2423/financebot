#!/usr/bin/env bash
# ==============================================================================
# Delilah Financial OS — Automated Bootstrap & Setup Script
# ==============================================================================
set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BOLD}======================================================${NC}"
echo -e "${BOLD}         Delilah Financial OS — Setup Script          ${NC}"
echo -e "${BOLD}======================================================${NC}"

# 1. Dependency Checks
echo -e "\n${BOLD}[1/6] Checking system prerequisites...${NC}"

has_command() {
    command -v "$1" >/dev/null 2>&1
}

MISSING=()
if ! has_command docker; then MISSING+=("docker"); fi
if ! has_command git; then MISSING+=("git"); fi
if ! has_command curl; then MISSING+=("curl"); fi

if [ ${#MISSING[@]} -ne 0 ]; then
    echo -e "${RED}Error: Missing required system dependencies: ${MISSING[*]}${NC}"
    echo "Please install them using your package manager (e.g., apt, dnf, brew) and re-run setup."
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo -e "${RED}Error: 'docker compose' (V2 plugin) is required but not installed.${NC}"
    exit 1
fi
echo -e "${GREEN}✓ All core prerequisites installed.${NC}"

# 2. Directory structure & Data volume initialization
echo -e "\n${BOLD}[2/6] Initializing local storage directories...${NC}"
mkdir -p data searxng .agents workspace
echo -e "${GREEN}✓ Directories 'data/', 'searxng/', and 'workspace/' verified.${NC}"

# 3. Environment file configuration (.env)
echo -e "\n${BOLD}[3/6] Configuring environment (.env)...${NC}"
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo -e "${YELLOW}! Created '.env' from '.env.example'.${NC}"
        echo -e "${YELLOW}! NOTE: Update your DISCORD_TOKEN and DISCORD_CHANNEL_ID in .env before running.${NC}"
    else
        echo -e "${RED}Error: .env.example not found.${NC}"
        exit 1
    fi
else
    echo -e "${GREEN}✓ Existing '.env' found.${NC}"
fi

# Ensure SANDBOX_INTERNAL_TOKEN is populated
if ! grep -q "^SANDBOX_INTERNAL_TOKEN=.\+" .env 2>/dev/null; then
    RAND_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || tr -dc A-Za-z0-9 </dev/urandom | head -c 64)
    if grep -q "^SANDBOX_INTERNAL_TOKEN=" .env; then
        sed -i "s/^SANDBOX_INTERNAL_TOKEN=.*/SANDBOX_INTERNAL_TOKEN=${RAND_TOKEN}/" .env
    else
        echo "SANDBOX_INTERNAL_TOKEN=${RAND_TOKEN}" >> .env
    fi
    echo -e "${GREEN}✓ Generated secure SANDBOX_INTERNAL_TOKEN.${NC}"
fi

# 4. SearXNG Configuration check
echo -e "\n${BOLD}[4/6] Verifying SearXNG search engine configuration...${NC}"
if [ ! -f searxng/settings.yml ]; then
    cat << 'EOF' > searxng/settings.yml
use_default_settings: true
server:
  secret_key: "delilah_searxng_internal_secret_key_change_in_prod"
  limiter: false
  image_proxy: true
search:
  safe_search: 0
  autocomplete: ""
  formats:
    - html
    - json
EOF
    echo -e "${GREEN}✓ Generated baseline 'searxng/settings.yml'.${NC}"
else
    echo -e "${GREEN}✓ 'searxng/settings.yml' verified.${NC}"
fi

# 5. Build and deploy container stack
echo -e "\n${BOLD}[5/6] Building and starting Docker services...${NC}"
docker compose down --remove-orphans || true
docker compose up -d --build

# 6. Service Health Check
echo -e "\n${BOLD}[6/6] Verifying container health...${NC}"
MAX_RETRIES=15
COUNT=0
HEALTHY=false

echo -n "Waiting for Delilah HTTP health endpoint... "
while [ $COUNT -lt $MAX_RETRIES ]; do
    if curl -s -f http://localhost:8000/health >/dev/null 2>&1; then
        HEALTHY=true
        break
    fi
    sleep 2
    echo -n "."
    COUNT=$((COUNT + 1))
done
echo ""

if [ "$HEALTHY" = true ]; then
    echo -e "${GREEN}✓ Delilah Financial OS is healthy and listening on http://localhost:8000!${NC}"
else
    echo -e "${YELLOW}! Delilah health check timed out. Check container logs with: docker logs -f delilah_bot${NC}"
fi

echo -e "\n${BOLD}======================================================${NC}"
echo -e "${GREEN}${BOLD}               Setup Complete!                        ${NC}"
echo -e "${BOLD}======================================================${NC}"
echo -e "Helpful commands:"
echo -e "  - View live logs:      ${BOLD}docker logs -f delilah_bot${NC}"
echo -e "  - Check status:        ${BOLD}docker compose ps${NC}"
echo -e "  - Run test suite:      ${BOLD}pytest${NC}"
echo -e "  - Restart services:    ${BOLD}docker compose restart${NC}"
echo -e "${BOLD}======================================================${NC}"
