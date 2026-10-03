#!/bin/bash
# EC2 user data (Amazon Linux 2023): installs Docker and starts the AI Game Arcade.
# Needs the IAM role "arcade-ec2-role" on the instance (Amazon Bedrock access). No API keys anywhere.
# Progress lines start with "ARCADE" in: EC2 -> Actions -> Monitor and troubleshoot -> Get system log
set -euxo pipefail

# ====== CHANGE ONLY THIS LINE ======
REPO_URL="https://github.com/YOUR_GITHUB_USER/ai-arcade.git"
# ===================================

SITE_DOMAIN="www.naeem.org.in"                     # your website address
AWS_REGION="ap-southeast-1"                        # Singapore
BEDROCK_MODEL_ID="global.amazon.nova-2-lite-v1:0"  # Amazon Nova 2 Lite

echo "ARCADE STEP 1/5: installing Docker and Git"
dnf install -y docker git
systemctl enable --now docker
usermod -aG docker ec2-user

echo "ARCADE STEP 2/5: installing Docker Compose"
# Pinned to v2.39.4: newer Compose needs buildx 0.17+, but Amazon Linux 2023 ships buildx 0.12.
ARCH=$(uname -m)
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL "https://github.com/docker/compose/releases/download/v2.39.4/docker-compose-linux-${ARCH}" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version

echo "ARCADE STEP 3/5: downloading code from GitHub"
rm -rf /opt/arcade
git clone "$REPO_URL" /opt/arcade
cd /opt/arcade
for f in docker-compose.yml Caddyfile Dockerfile app/app.py; do
  test -f "$f" || { echo "ARCADE ERROR: $f is missing from the GitHub repo"; exit 1; }
done

cat > .env <<ENV
AI_MODE=bedrock
SITE_DOMAIN=$SITE_DOMAIN
AWS_REGION=$AWS_REGION
BEDROCK_MODEL_ID=$BEDROCK_MODEL_ID
REDIS_URL=redis://redis:6379/0
CACHE_TTL=3600
WORKSHOP_TITLE=Cloud Meets AI
ENV

echo "ARCADE STEP 4/5: building the app image"
docker build -t ai-arcade:latest .

echo "ARCADE STEP 5/5: starting the arcade"
docker compose up -d --no-build

for i in $(seq 1 30); do
  if curl -fs http://127.0.0.1:8000/api/health; then echo; echo "ARCADE READY"; exit 0; fi
  sleep 2
done
echo "ARCADE ERROR: app did not start"; docker compose logs --tail 50
exit 1
