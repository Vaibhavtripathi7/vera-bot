#!/usr/bin/env bash
# Redeploy from your laptop:  bash deploy/push.sh ubuntu@<vm-ip>
set -euo pipefail
HOST="$1"
cd "$(dirname "$0")/.."
rsync -az --delete --exclude .venv --exclude .git --exclude '*.db*' --exclude eval/out --exclude expanded ./ "$HOST":~/vera-src/
ssh "$HOST" 'sudo rsync -a --delete --exclude .venv ~/vera-src/ /opt/vera/ && sudo chown -R vera:vera /opt/vera \
  && sudo -u vera bash -c "cd /opt/vera && ~/.local/bin/uv sync --no-dev --python 3.12 -q" && sudo systemctl restart vera \
  && sleep 2 && curl -fsS http://127.0.0.1:8080/v1/healthz'
