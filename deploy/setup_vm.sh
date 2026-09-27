#!/usr/bin/env bash
# One-time VM setup (Ubuntu 22.04/24.04). Run on the VM from the repo copy:  sudo bash deploy/setup_vm.sh [domain]
# Default domain = <public-ip>.sslip.io (free, no signup; Caddy gets a Let's Encrypt cert automatically).
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
IP="$(curl -fsS https://api.ipify.org)"
DOMAIN="${1:-${IP//./-}.sslip.io}"

apt-get update -y
apt-get install -y curl rsync debian-keyring debian-archive-keyring apt-transport-https gnupg iptables-persistent
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi

id vera >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin vera
mkdir -p /opt/vera /var/lib/vera
rsync -a --delete --exclude .venv --exclude .git --exclude '*.db*' --exclude eval/out "$SRC"/ /opt/vera/
chown -R vera:vera /opt/vera /var/lib/vera

sudo -u vera bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' >/dev/null
sudo -u vera bash -c 'cd /opt/vera && ~/.local/bin/uv sync --no-dev --python 3.12'

[ -f /etc/vera.env ] || install -m 600 -o vera -g vera /opt/vera/deploy/vera.env.example /etc/vera.env
install -m 644 /opt/vera/deploy/vera.service /etc/systemd/system/vera.service
sed "s/__DOMAIN__/$DOMAIN/" /opt/vera/deploy/Caddyfile.template > /etc/caddy/Caddyfile

# Oracle Cloud Ubuntu images block 80/443 in iptables by default (also open them in the VCN security list!)
iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || iptables -I INPUT 5 -p tcp --dport 80 -j ACCEPT
iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || iptables -I INPUT 5 -p tcp --dport 443 -j ACCEPT
netfilter-persistent save || true

systemctl daemon-reload
systemctl enable --now vera
systemctl restart caddy
sleep 3
curl -fsS http://127.0.0.1:8080/v1/healthz && echo
echo "Edit keys:   sudo nano /etc/vera.env && sudo systemctl restart vera"
echo "Public URL:  https://$DOMAIN   (check: curl https://$DOMAIN/v1/healthz)"
