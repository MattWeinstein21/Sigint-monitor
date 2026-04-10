#!/bin/bash
# deploy.sh — Run this on your server to pull latest changes and restart
#
# First-time setup:
#   cd /opt/sigint
#   git clone https://github.com/YOUR_USERNAME/sigint-monitor.git .
#
# After that, just run:
#   /opt/sigint/deploy.sh

set -e

INSTALL_DIR="/opt/sigint"
cd "$INSTALL_DIR"

echo "═══ SIGINT Deploy ═══"

# Pull latest
echo "→ Pulling latest from GitHub..."
git pull origin main

# Copy frontend into place
echo "→ Updating frontend..."
cp -f index.html static/index.html

# Update Python deps if requirements changed
echo "→ Checking dependencies..."
source venv/bin/activate
pip install -q -r requirements.txt 2>/dev/null || true

# Restart service
echo "→ Restarting service..."
systemctl restart sigint

# Verify
sleep 2
if systemctl is-active --quiet sigint; then
    echo "✓ SIGINT is running"
    curl -s http://localhost:5000/api/status | python3 -m json.tool
else
    echo "✗ Service failed to start — check: journalctl -u sigint -n 30"
    exit 1
fi

echo "═══ Deploy complete ═══"
