#!/usr/bin/env bash
# Daily safe backup of the bot's SQLite database.
# - Uses SQLite backup API (WAL-safe, consistent snapshot while the bot runs).
# - Verifies integrity; aborts and removes the copy if the check fails.
# - Pushes the copy to the private off-server repo germani-bot-backups.
# - Prunes local copies older than 14 days (GitHub archive is permanent).
set -euo pipefail

STAMP=$(date +%Y%m%d_%H%M%S)
BK=/opt/germani-bot/db-backups
CONTAINER=${CONTAINER:-germani-bot}
REPO=${BACKUP_REPO:-eliasajeep1966-gif/germani-bot-backups}
TOKEN_FILE=${BACKUP_TOKEN_FILE:-/root/.github_backup_token}

# Pull remote state first so pushes are fast-forward on cron reruns.
git -C "$BK" pull -q origin main 2>/dev/null || true

docker exec -i "$CONTAINER" python - "$STAMP" <<'PY'
import sqlite3, sys, os
stamp = sys.argv[1]
dst = f'/var/lib/bot/backups/bot_database_{stamp}.db'
os.makedirs('/var/lib/bot/backups', exist_ok=True)
c = sqlite3.connect('/var/lib/bot/bot_database.db')
b = sqlite3.connect(dst)
c.backup(b)
b.close()
chk = sqlite3.connect(dst)
ok = chk.execute('PRAGMA integrity_check').fetchone()[0]
chk.close(); c.close()
print('integrity_check:', ok)
if ok != 'ok':
    os.remove(dst); sys.exit(1)
PY

cp -a "/var/lib/docker/volumes/germani_bot_runtime/_data/backups/bot_database_${STAMP}.db" "$BK/"

cd "$BK"
if [ ! -d .git ]; then
  git init -q
  git config user.email "bot-backup@germani-bot.local"
  git config user.name "germani-bot backup"
  git remote add origin "https://x-access-token:$(cat "$TOKEN_FILE")@github.com/${REPO}.git"
fi
# Cron clones start without a checked-out branch; always aim at main.
git symbolic-ref HEAD refs/heads/main
git add "bot_database_${STAMP}.db"
git commit -qm "backup ${STAMP}"
git push -q origin main

# Local retention only; GitHub keeps every copy permanently.
find "$BK" -maxdepth 1 -name 'bot_database_*.db' -mtime +14 -delete
echo "OK backup_${STAMP}"
