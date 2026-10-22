#!/bin/bash
set -euo pipefail

systemctl enable ssh
systemctl start ssh

# cloud-init normally emits host-key fingerprints only on the first boot.
# EC2's finite console buffer can lose them after a creator reboots a builder.
# Keep the authenticated console source usable without trusting ssh-keyscan or
# changing the host keys, SSH policy, or any lab service.
cat > /etc/systemd/system/secforge-builder-host-identity.service <<'EOF'
[Unit]
Description=Report builder public SSH fingerprint to the EC2 console
After=ssh.service cloud-init.service

[Service]
Type=oneshot
ExecStart=/usr/bin/ssh-keygen -E sha256 -lf /etc/ssh/ssh_host_ed25519_key.pub
# journald console forwarding may target a different console. EC2 captures ttyS0.
StandardOutput=tty
TTYPath=/dev/ttyS0
TTYReset=no
TTYVHangup=no
StandardError=journal
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 /etc/systemd/system/secforge-builder-host-identity.service
systemctl daemon-reload
systemctl enable --now secforge-builder-host-identity.service
