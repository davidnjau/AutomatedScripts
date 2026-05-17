#!/bin/bash
# migrate.sh — Copy running bot and its data to a new server
# Usage: ./migrate.sh

set -e

# ── Prompt for target server details ──────────────────────
read -p "New server IP address: " TARGET_HOST
read -p "New server username:   " TARGET_USER
read -s -p "New server password:   " TARGET_PASS
echo ""

BOT_DIR="/opt/ardhisasa-bot"
BACKUP_FILE="/tmp/bot_data.tar.gz"

echo ""
echo "=== Ardhisasa Bot Migration ==="
echo "Target: ${TARGET_USER}@${TARGET_HOST}:${BOT_DIR}"
echo ""

# ── Check sshpass is available ────────────────────────────
if ! command -v sshpass &>/dev/null; then
    echo "Installing sshpass..."
    apt-get install -y sshpass 2>/dev/null || \
    yum install -y sshpass 2>/dev/null || \
    brew install sshpass 2>/dev/null || \
    { echo "ERROR: sshpass not found. Install it and retry."; exit 1; }
fi

SSH="sshpass -p ${TARGET_PASS} ssh -o StrictHostKeyChecking=no ${TARGET_USER}@${TARGET_HOST}"
SCP="sshpass -p ${TARGET_PASS} scp -o StrictHostKeyChecking=no"

# ── Step 1: Export Docker volume from current server ──────
echo "[1/5] Exporting bot data volume..."
docker run --rm \
    -v bot_data:/data \
    -v /tmp:/backup \
    alpine tar czf /backup/bot_data.tar.gz -C /data .
echo "      Done — saved to ${BACKUP_FILE}"

# ── Step 2: Copy files to new server ─────────────────────
echo "[2/5] Copying bot files to new server..."
$SSH "mkdir -p ${BOT_DIR}"
$SCP -r ${BOT_DIR}/. ${TARGET_USER}@${TARGET_HOST}:${BOT_DIR}/
echo "      Done"

echo "[3/5] Copying data backup to new server..."
$SCP ${BACKUP_FILE} ${TARGET_USER}@${TARGET_HOST}:/tmp/
echo "      Done"

# ── Step 3: Restore volume and start bot on new server ───
echo "[4/5] Restoring data and starting bot on new server..."
$SSH bash <<EOF
  set -e

  # Restore data volume
  docker volume create bot_data 2>/dev/null || true
  docker run --rm \
      -v bot_data:/data \
      -v /tmp:/backup \
      alpine tar xzf /backup/bot_data.tar.gz -C /data

  # Install Docker Compose if missing
  if ! command -v docker &>/dev/null; then
      echo "Docker not found — please install Docker on the new server first."
      exit 1
  fi

  # Start the bot
  cd ${BOT_DIR}
  docker compose pull 2>/dev/null || true
  docker compose up --build -d
  sleep 3
  docker compose ps
EOF
echo "      Done"

# ── Step 4: Stop bot on current server ───────────────────
echo "[5/5] Stopping bot on current server..."
cd ${BOT_DIR}
docker compose down
echo "      Done"

echo ""
echo "=== Migration complete ==="
echo "Bot is now running on ${TARGET_HOST}"
echo "Verify with: ssh ${TARGET_USER}@${TARGET_HOST} 'docker compose -f ${BOT_DIR}/docker-compose.yml logs --tail=20 ardhisasa-bot'"
