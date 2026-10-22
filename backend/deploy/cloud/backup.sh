#!/bin/bash
set -euo pipefail
umask 077
# Root-only local recovery set. Never served by Caddy. No secrets in argv.
# Atomic publication: a recovery set is usable only when COMPLETE exists.
base=/var/backups/secforge
root=/var/lib/secforge/avatars
[[ $EUID -eq 0 && -d "$base" && ! -L "$base" && -d "$root" && ! -L "$root" ]]
[[ $(stat -c %a "$base") == 700 && -f "$root/.backup.lock" && ! -L "$root/.backup.lock" ]]
stamp=$(date -u +%Y%m%dT%H%M%SZ)
stage=$(mktemp -d "$base/.recovery-${stamp}-XXXXXX")
# Failed sets remain private for diagnosis and are never labelled complete.
exec 9<> "$root/.backup.lock"
flock -w 15 9
runuser -u postgres -- /usr/lib/postgresql/18/bin/pg_dump --format=custom --no-owner --no-acl secforge > "$stage/database.dump"
/usr/lib/postgresql/18/bin/pg_restore --list "$stage/database.dump" >/dev/null
find "$root" -maxdepth 1 -type f -regextype posix-extended -regex '.*/[a-f0-9]{32}\.png' -printf '%f\0' | tar --null --verbatim-files-from -C "$root" -cf "$stage/avatars.tar" -T -
tar -tf "$stage/avatars.tar" >/dev/null
# Lesson files share the avatar backup lock and are never web-server static files.
media="$root/room-media"
if [[ -d "$media" && ! -L "$media" ]]; then
    find "$media" -maxdepth 1 -type f -regextype posix-extended -regex '.*/[a-f0-9]{32}\.(png|jpg|webp)' -printf '%f\0' | tar --null --verbatim-files-from -C "$media" -cf "$stage/room-media.tar" -T -
else
    [[ ! -e "$media" && ! -L "$media" ]]
    tar -cf "$stage/room-media.tar" --files-from /dev/null
fi
tar -tf "$stage/room-media.tar" >/dev/null
flock -u 9
# Contains secrets: keep root-private, copy off-instance only over pinned SSH.
tar -C / -cf "$stage/configuration.tar" \
    etc/secforge etc/caddy/Caddyfile etc/systemd/system/secforge.service \
    etc/systemd/system/secforge-backup.service etc/systemd/system/secforge-backup.timer \
    usr/local/sbin/secforge-backup opt/secforge/backend/requirements.txt
(cd "$stage" && sha256sum database.dump avatars.tar room-media.tar configuration.tar > SHA256SUMS && sha256sum --check --status SHA256SUMS)
printf 'SecForge recovery set v2; UTC %s\n' "$stamp" > "$stage/COMPLETE"
final="$base/recovery-${stamp}-${stage##*-}"
mv -T "$stage" "$final"
# Only complete root-owned recovery sets created by this utility are retained for
# seven days; release rollback directories and unrelated backups are untouched.
find "$base" -mindepth 1 -maxdepth 1 -type d -name 'recovery-20?????????????Z-*' -mtime +7 -user root -exec test -f '{}/COMPLETE' \; -exec rm -rf -- '{}' \;
# Preserve all pre-existing dump/avatar files and release backups. Only this
# utility's completed recovery sets are eligible for automated retention.
