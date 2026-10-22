#!/bin/bash
# Run only on the dedicated new Ubuntu application server, without secrets.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg python3-venv python3-pip postgresql-common openssh-client
# Official PGDG packages keep the PostgreSQL 18 dump/restore version compatible.
/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
apt-get install -y -qq postgresql-18
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -qq
apt-get install -y -qq caddy
systemctl stop caddy
id secforge >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/secforge --shell /usr/sbin/nologin secforge
install -d -o secforge -g secforge -m 700 /var/lib/secforge/builders /var/lib/secforge/labs /var/lib/secforge/terraform-plugins /var/lib/secforge/.ssh
install -d -o root -g secforge -m 750 /etc/secforge /etc/secforge/keys
install -d -o root -g root -m 700 /var/backups/secforge
install -d -o root -g root -m 755 /opt/secforge /srv/secforge/frontend
# Bounded burst workload safety on the 2GiB server; CPU credits use standard mode.
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
systemctl enable postgresql
