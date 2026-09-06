#!/usr/bin/env bash
# Self-update helper, triggered by the bot's /update command.
#
# IMPORTANT: this script recreates the very container the bot runs in, so it
# MUST detach from the bot process first (the bot calls it with setsid+nohup).
# It writes progress to UPDATE_LOG and a one-line outcome to UPDATE_RESULT,
# which the bot reads and reports after it comes back up.
set -uo pipefail

REPO_DIR="${REPO_DIR:-/app}"
BRANCH="${UPDATE_BRANCH:-arena/01a0749f-animexindow}"
SERVICE="${COMPOSE_SERVICE:-telegram_bot}"
LOG="${UPDATE_LOG:-$REPO_DIR/update.log}"
RESULT="${UPDATE_RESULT:-$REPO_DIR/update_result.json}"

exec >>"$LOG" 2>&1
echo "===== update started $(date -u +%FT%TZ) ====="

finish() {  # finish <ok|fail> <message>
    printf '{"status":"%s","msg":%s,"time":"%s","chat":"%s","msg_id":"%s"}\n' \
        "$1" "$(printf '%s' "$2" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
        "$(date -u +%FT%TZ)" "${UPDATE_CHAT:-}" "${UPDATE_MSG_ID:-}" > "$RESULT"
    echo "----- $1: $2"
    exit 0
}

cd "$REPO_DIR" || finish fail "repo dir $REPO_DIR not found"

# Refuse to rebuild without credentials - otherwise the container would come
# back up with placeholder tokens and silently fail to log in.
if [ ! -f "$REPO_DIR/.env" ]; then
    finish fail ".env is missing in $REPO_DIR - copy .env.example to .env and fill it in"
fi

# --- pick a compose command (v2 plugin or legacy v1) ---
if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
else
    finish fail "neither 'docker compose' nor 'docker-compose' is available"
fi

if ! docker info >/dev/null 2>&1; then
    finish fail "cannot talk to the Docker daemon (is /var/run/docker.sock mounted?)"
fi

# --- 1. fetch the new code ---
OLD_REV="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "current revision: $OLD_REV"

git config --global --add safe.directory "$REPO_DIR" 2>/dev/null

if ! git fetch --prune origin "$BRANCH"; then
    finish fail "git fetch failed (check network / credentials)"
fi

# Keep local runtime files, take the remote code verbatim.
# Back up local runtime files that must survive a hard reset.
BAK="$(mktemp -d)"
for keep in .env channels.json processed_posts.json; do
    [ -f "$keep" ] && cp -a "$keep" "$BAK/" 2>/dev/null
done

git stash push --include-untracked -m "auto-update $(date -u +%FT%TZ)" >/dev/null 2>&1
if ! git reset --hard "origin/$BRANCH"; then
    finish fail "git reset to origin/$BRANCH failed"
fi

# Restore the protected files after the reset.
for keep in .env channels.json processed_posts.json; do
    [ -f "$BAK/$keep" ] && cp -a "$BAK/$keep" "$REPO_DIR/$keep" 2>/dev/null
done
rm -rf "$BAK"

NEW_REV="$(git rev-parse --short HEAD)"
echo "new revision: $NEW_REV"

if [ "$OLD_REV" = "$NEW_REV" ] && [ "${FORCE_UPDATE:-0}" != "1" ]; then
    finish ok "Already up to date at $NEW_REV - nothing to rebuild."
fi

CHANGES="$(git log --oneline "$OLD_REV..$NEW_REV" 2>/dev/null | head -10)"
[ -z "$CHANGES" ] && CHANGES="(no readable changelog)"
echo "$CHANGES"

# --- 2. syntax-check before we throw away the working container ---
if command -v python3 >/dev/null 2>&1; then
    if ! python3 -m py_compile bot.py; then
        git reset --hard "$OLD_REV"
        finish fail "new bot.py has a syntax error - rolled back to $OLD_REV"
    fi
fi

# --- 3. rebuild ---
echo "building image..."
if ! $DC build --pull "$SERVICE"; then
    echo "build failed, rolling back"
    git reset --hard "$OLD_REV"
    $DC build "$SERVICE" >/dev/null 2>&1
    finish fail "docker build failed - rolled back to $OLD_REV"
fi

# --- 4. record success BEFORE restarting (we die during the restart) ---
printf '{"status":"ok","msg":%s,"time":"%s","chat":"%s","msg_id":"%s"}\n' \
    "$(printf 'Updated %s -> %s\n%s' "$OLD_REV" "$NEW_REV" "$CHANGES" \
       | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
    "$(date -u +%FT%TZ)" "${UPDATE_CHAT:-}" "${UPDATE_MSG_ID:-}" > "$RESULT"

# --- 5. recreate the container (this kills us mid-command - expected) ---
echo "recreating container..."
$DC up -d --force-recreate "$SERVICE"

# --- 6. clean up old/dangling images so the VPS disk does not fill ---
echo "pruning old images..."
docker image prune -f
docker builder prune -f --keep-storage 512MB 2>/dev/null || docker builder prune -f
echo "===== update finished $(date -u +%FT%TZ) ====="
