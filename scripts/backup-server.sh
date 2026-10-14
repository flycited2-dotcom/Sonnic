#!/bin/sh
# Create a consistent Sonnic snapshot, preserving container identities/network namespace.
set -eu
umask 077
snapshot_name=${1:?snapshot name required}
case "$snapshot_name" in
  scheduled-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z) ;;
  *) echo 'invalid snapshot name' >&2; exit 2 ;;
esac
cd /opt/sonnic/source/deploy
exec 9>/opt/sonnic/backup.lock
flock -n 9 || { echo 'backup already running' >&2; exit 3; }
# Refuse maintenance if the application was already stopped or partially running.
for service in api web gateway; do
  test "$(docker inspect -f '{{.State.Running}}' "sonnic-$service-1")" = true
done
test "$(df -Pk /opt/sonnic | awk 'NR==2 {print $4}')" -gt 1048576
restore() {
  docker compose start api web gateway >&2
}
trap restore EXIT
trap 'exit 130' INT TERM
# Freeze new ingress before checking leases; an active worker causes a clean skip.
docker compose stop -t 20 gateway >&2
docker exec -i sonnic-api-1 python - <<'PY'
import sqlite3, sys, time
db=sqlite3.connect('file:/data/synthetic.sqlite3.bridge.sqlite3?mode=ro',uri=True)
active=db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','leased','awaiting_review') AND deadline>?",(time.time(),)).fetchone()[0]
if active:
    print('backup deferred: active jobs', file=sys.stderr)
    sys.exit(4)
PY
docker compose stop -t 20 web api >&2
python3 /opt/sonnic/backup_sonnic.py create \
  --main /var/lib/docker/volumes/sonnic_data/_data/synthetic.sqlite3 \
  --destination "/opt/sonnic/backups/$snapshot_name" --api-stopped
# EXIT trap restarts the same containers even when backup or verification fails.
