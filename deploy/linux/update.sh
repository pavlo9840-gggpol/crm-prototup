#!/usr/bin/env bash
set -Eeuo pipefail
INSTALL_ROOT="/opt/autozapaz"
APP_LINK="$INSTALL_ROOT/app"
VENV_LINK="$INSTALL_ROOT/venv"
RELEASES_DIR="$INSTALL_ROOT/releases"
VENVS_DIR="$INSTALL_ROOT/venvs"
DATA_DIR="/var/lib/autozapaz"
BACKUP_DIR="$DATA_DIR/backups"
ENV_FILE="/etc/autozapaz/autozapaz.env"
SERVICE="autozapaz.service"
SERVICE_USER="autozapaz"
HEALTH_URL="http://127.0.0.1:8000/login/"

fail() { printf 'Помилка оновлення: %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> %s\n' "$*"; }
[[ "$(id -u)" -eq 0 ]] || fail "Запустіть від root: sudo bash deploy/linux/update.sh <каталог-релізу> <версія>"
[[ $# -eq 2 ]] || fail "Використання: update.sh <каталог-релізу> <версія>"
RELEASE_SOURCE="$(realpath -e -- "$1")"
VERSION="$2"
[[ "$VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || fail "Некоректний ідентифікатор версії."
[[ -d "$RELEASE_SOURCE" && -f "$RELEASE_SOURCE/manage.py" && -f "$RELEASE_SOURCE/requirements.txt" ]] || fail "Каталог релізу має містити manage.py та requirements.txt."
[[ -f "$ENV_FILE" && -f "$APP_LINK/manage.py" && -x "$VENV_LINK/bin/python" ]] || fail "Не знайдено чинну інсталяцію АвтоЗапас."
[[ -d "$DATA_DIR" ]] || fail "Не знайдено постійний каталог даних."
id "$SERVICE_USER" >/dev/null 2>&1 || fail "Не знайдено системного користувача $SERVICE_USER."
systemctl cat "$SERVICE" >/dev/null 2>&1 || fail "Службу $SERVICE не встановлено."
CURRENT_APP_REAL="$(readlink -f -- "$APP_LINK")"
case "$RELEASE_SOURCE/" in "$CURRENT_APP_REAL/"*) fail "Новий реліз не може бути всередині чинної програми." ;; esac
[[ "$(find "$RELEASE_SOURCE" -type l -print -quit)" == "" ]] || fail "Реліз містить символічні посилання."
[[ "$(find "$RELEASE_SOURCE" ! -type d ! -type f -print -quit)" == "" ]] || fail "Реліз містить спеціальні файли."

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
[[ "$AUTOZAPAZ_DATA_DIR" == "$DATA_DIR" ]] || fail "Оновлювач підтримує стандартний AUTOZAPAZ_DATA_DIR=$DATA_DIR."
BACKUP_DIR="$AUTOZAPAZ_BACKUP_DIR"
[[ -d "$BACKUP_DIR" ]] || fail "Не знайдено каталог резервних копій: $BACKUP_DIR"
: "${DJANGO_SECRET_KEY:?У конфігурації немає DJANGO_SECRET_KEY}"
: "${AUTOZAPAZ_DATA_DIR:?У конфігурації немає AUTOZAPAZ_DATA_DIR}"
: "${AUTOZAPAZ_BACKUP_DIR:?У конфігурації немає AUTOZAPAZ_BACKUP_DIR}"
: "${AUTOZAPAZ_LICENSE_PUBLIC_KEY:?У конфігурації немає AUTOZAPAZ_LICENSE_PUBLIC_KEY}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RELEASE_ID="${VERSION}-${STAMP}"
NEW_APP="$RELEASES_DIR/$RELEASE_ID"
NEW_VENV="$VENVS_DIR/$RELEASE_ID"
OLD_APP_REAL="$(readlink -f -- "$APP_LINK")"
OLD_VENV_REAL="$(readlink -f -- "$VENV_LINK")"
OLD_APP_FALLBACK="$RELEASES_DIR/rollback-$STAMP"
OLD_VENV_FALLBACK="$VENVS_DIR/rollback-$STAMP"
RESTORE_DIR="$DATA_DIR/.rollback-restore-$STAMP"
BACKUP_PATH=""
APP_SWITCHED=0
VENV_SWITCHED=0
SERVICE_STOPPED=0
UPDATE_COMPLETE=0
ROLLBACK_ATTEMPTED=0
[[ ! -e "$NEW_APP" && ! -e "$NEW_VENV" ]] || fail "Каталог версії вже існує."
mkdir -p "$RELEASES_DIR" "$VENVS_DIR"

as_service() {
    runuser -u "$SERVICE_USER" -- env \
        "DJANGO_SECRET_KEY=$DJANGO_SECRET_KEY" \
        "DJANGO_DEBUG=${DJANGO_DEBUG:-0}" \
        "DJANGO_ALLOWED_HOSTS=${DJANGO_ALLOWED_HOSTS:-localhost,127.0.0.1,[::1]}" \
        "DJANGO_CSRF_TRUSTED_ORIGINS=${DJANGO_CSRF_TRUSTED_ORIGINS:-}" \
        "AUTOZAPAZ_DATA_DIR=$AUTOZAPAZ_DATA_DIR" \
        "AUTOZAPAZ_BACKUP_DIR=$AUTOZAPAZ_BACKUP_DIR" \
        "AUTOZAPAZ_LICENSE_PUBLIC_KEY=$AUTOZAPAZ_LICENSE_PUBLIC_KEY" \
        "AUTOZAPAZ_REQUIRE_LICENSE=${AUTOZAPAZ_REQUIRE_LICENSE:-1}" \
        "AUTOZAPAZ_SECURE_COOKIES=${AUTOZAPAZ_SECURE_COOKIES:-0}" "$@"
}
atomic_link() {
    local link_path="$1" target_path="$2" temporary_link="${1}.next-$STAMP"
    ln -s "$target_path" "$temporary_link"
    if [[ -L "$link_path" ]]; then
        mv -Tf "$temporary_link" "$link_path"
    else
        local fallback="$3"
        [[ -e "$link_path" ]] || fail "Не знайдено каталог для перемикання: $link_path"
        mv "$link_path" "$fallback"
        mv -Tf "$temporary_link" "$link_path"
    fi
}
health_check() {
    local attempt
    for attempt in $(seq 1 30); do
        if systemctl is-active --quiet "$SERVICE" && python3 - "$HEALTH_URL" <<'PY'
import sys
import urllib.error
import urllib.request
try:
    request = urllib.request.Request(sys.argv[1], headers={"User-Agent": "AutoZapaz-Updater/1"})
    with urllib.request.urlopen(request, timeout=3) as response:
        if 200 <= response.status < 400:
            raise SystemExit(0)
except (urllib.error.URLError, TimeoutError, OSError):
    pass
raise SystemExit(1)
PY
        then return 0; fi
        sleep 2
    done
    return 1
}
rollback_update() {
    local reason="$1"
    ROLLBACK_ATTEMPTED=1
    printf '\nПОМИЛКА: %s\nПовертаю попередню версію.\n' "$reason" >&2
    systemctl stop "$SERVICE" >/dev/null 2>&1 || true
    if [[ "$APP_SWITCHED" -eq 1 || "$VENV_SWITCHED" -eq 1 ]]; then
        if [[ "$APP_SWITCHED" -eq 1 ]]; then
            ln -s "$OLD_APP_REAL" "${APP_LINK}.rollback-$STAMP"
            mv -Tf "${APP_LINK}.rollback-$STAMP" "$APP_LINK"
        fi
        if [[ "$VENV_SWITCHED" -eq 1 ]]; then
            ln -s "$OLD_VENV_REAL" "${VENV_LINK}.rollback-$STAMP"
            mv -Tf "${VENV_LINK}.rollback-$STAMP" "$VENV_LINK"
        fi
        if [[ -d "$RESTORE_DIR" && -f "$RESTORE_DIR/db.sqlite3" ]]; then
            [[ ! -e "$DATA_DIR/db.sqlite3" ]] || mv "$DATA_DIR/db.sqlite3" "$DATA_DIR/db.sqlite3.failed-$STAMP"
            [[ ! -e "$DATA_DIR/db.sqlite3-wal" ]] || mv "$DATA_DIR/db.sqlite3-wal" "$DATA_DIR/db.sqlite3-wal.failed-$STAMP"
            [[ ! -e "$DATA_DIR/db.sqlite3-shm" ]] || mv "$DATA_DIR/db.sqlite3-shm" "$DATA_DIR/db.sqlite3-shm.failed-$STAMP"
            cp "$RESTORE_DIR/db.sqlite3" "$DATA_DIR/db.sqlite3.rollback-$STAMP"
            chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR/db.sqlite3.rollback-$STAMP"
            chmod 0600 "$DATA_DIR/db.sqlite3.rollback-$STAMP"
            mv "$DATA_DIR/db.sqlite3.rollback-$STAMP" "$DATA_DIR/db.sqlite3"
            [[ ! -e "$DATA_DIR/media" ]] || mv "$DATA_DIR/media" "$DATA_DIR/media.failed-$STAMP"
            if [[ -d "$RESTORE_DIR/media" ]]; then
                mv "$RESTORE_DIR/media" "$DATA_DIR/media"
            else
                install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$DATA_DIR/media"
            fi
            chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR/media"
            chmod 0700 "$DATA_DIR/media"
        fi
    fi
    systemctl start "$SERVICE" || {
        printf 'Попередній код повернуто, але служба не стартувала. Дивіться journalctl -u %s.\n' "$SERVICE" >&2
        return 1
    }
    if ! health_check; then
        printf 'Попередня версія не пройшла health-check. Backup: %s\n' "$BACKUP_PATH" >&2
        return 1
    fi
    printf 'Попередню версію та дані відновлено. Backup: %s\n' "$BACKUP_PATH"
}
on_exit() {
    local exit_code="$?"
    trap - EXIT
    if [[ "$UPDATE_COMPLETE" -eq 1 || "$exit_code" -eq 0 ]]; then
        exit "$exit_code"
    fi
    if [[ "$ROLLBACK_ATTEMPTED" -eq 1 ]]; then
        exit "$exit_code"
    fi
    if [[ "$APP_SWITCHED" -eq 1 || "$VENV_SWITCHED" -eq 1 ]]; then
        rollback_update "Неочікувана помилка (код $exit_code) під час оновлення." || true
    elif [[ "$SERVICE_STOPPED" -eq 1 ]]; then
        printf '\nНеочікувана помилка (код %s). Запускаю чинну службу.\n' "$exit_code" >&2
        if systemctl start "$SERVICE"; then
            SERVICE_STOPPED=0
            health_check || printf 'Служба запущена, але HTTP health-check не пройшов. Перевірте journalctl -u %s.\n' "$SERVICE" >&2
        else
            printf 'Не вдалося запустити службу. Перевірте journalctl -u %s.\n' "$SERVICE" >&2
        fi
    fi
    exit "$exit_code"
}
trap on_exit EXIT

info "Підготовка релізу $VERSION"
cp -a "$RELEASE_SOURCE" "$NEW_APP"
chown -R "root:$SERVICE_USER" "$NEW_APP"
find "$NEW_APP" -type d -exec chmod 0750 {} +
find "$NEW_APP" -type f -exec chmod 0640 {} +
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$NEW_APP/staticfiles"
python3 -m venv "$NEW_VENV"
"$NEW_VENV/bin/python" -m pip install --upgrade pip
"$NEW_VENV/bin/python" -m pip install -r "$NEW_APP/requirements.txt"
chown -R "root:$SERVICE_USER" "$NEW_VENV"
find "$NEW_VENV" -type d -exec chmod 0750 {} +

info "Перевірка нового коду до зупинки служби"
as_service "$NEW_VENV/bin/python" "$NEW_APP/manage.py" check --database default
as_service "$NEW_VENV/bin/python" "$NEW_APP/manage.py" migrate --plan
as_service "$NEW_VENV/bin/python" "$NEW_APP/manage.py" collectstatic --noinput

info "Зупинка служби та резервне копіювання"
systemctl stop "$SERVICE"
SERVICE_STOPPED=1
if ! BACKUP_OUTPUT="$(as_service "$OLD_VENV_REAL/bin/python" "$APP_LINK/manage.py" backup --keep 30 2>&1)"; then
    systemctl start "$SERVICE" || true
    SERVICE_STOPPED=0
    fail "Не вдалося створити резервну копію; оновлення скасовано. $BACKUP_OUTPUT"
fi
printf '%s\n' "$BACKUP_OUTPUT"
BACKUP_PATH="$(printf '%s\n' "$BACKUP_OUTPUT" | sed -n 's/^Saved //p' | tail -n 1)"
[[ -n "$BACKUP_PATH" && -f "$BACKUP_PATH" ]] || {
    systemctl start "$SERVICE" || true
    SERVICE_STOPPED=0
    fail "Команда backup не повернула шлях до архіву."
}
case "$(realpath -e -- "$BACKUP_PATH")" in
    "$(realpath -e -- "$BACKUP_DIR")"/*) ;;
    *) systemctl start "$SERVICE" || true; SERVICE_STOPPED=0; fail "Backup створено поза каталогом резервних копій." ;;
esac
if ! as_service "$OLD_VENV_REAL/bin/python" "$APP_LINK/manage.py" verify_backup "$BACKUP_PATH"; then
    systemctl start "$SERVICE" || true
    SERVICE_STOPPED=0
    fail "Перевірка резервної копії не пройшла; оновлення скасовано."
fi
if ! as_service "$OLD_VENV_REAL/bin/python" "$APP_LINK/manage.py" restore_backup "$BACKUP_PATH" "$RESTORE_DIR" --apply; then
    systemctl start "$SERVICE" || true
    SERVICE_STOPPED=0
    fail "Не вдалося підготувати каталог відкату; оновлення скасовано."
fi

info "Перемикання коду та Python-середовища"
atomic_link "$APP_LINK" "$NEW_APP" "$OLD_APP_FALLBACK"
if [[ "$OLD_APP_REAL" == "$APP_LINK" ]]; then OLD_APP_REAL="$OLD_APP_FALLBACK"; fi
APP_SWITCHED=1
atomic_link "$VENV_LINK" "$NEW_VENV" "$OLD_VENV_FALLBACK"
if [[ "$OLD_VENV_REAL" == "$VENV_LINK" ]]; then OLD_VENV_REAL="$OLD_VENV_FALLBACK"; fi
VENV_SWITCHED=1

info "Застосування міграцій"
if ! as_service "$VENV_LINK/bin/python" "$APP_LINK/manage.py" migrate --noinput; then
    rollback_update "Міграції завершилися помилкою." || true
    fail "Оновлення скасовано."
fi
if ! as_service "$VENV_LINK/bin/python" "$APP_LINK/manage.py" check --database default; then
    rollback_update "Django system check не пройшов." || true
    fail "Оновлення скасовано."
fi
if ! as_service "$VENV_LINK/bin/python" "$APP_LINK/manage.py" collectstatic --noinput; then
    rollback_update "collectstatic завершився помилкою." || true
    fail "Оновлення скасовано."
fi

info "Запуск і health-check"
if ! systemctl start "$SERVICE" || ! health_check; then
    rollback_update "Нова версія не запустилася або не відповідає на HTTP." || true
    fail "Оновлення скасовано."
fi
SERVICE_STOPPED=0
rm -rf -- "$RESTORE_DIR"
UPDATE_COMPLETE=1
printf '\nОновлення до %s успішне.\n' "$VERSION"
printf 'Backup перед оновленням: %s\n' "$BACKUP_PATH"
printf 'Попередній код і venv збережені: %s ; %s\n' "$OLD_APP_REAL" "$OLD_VENV_REAL"
printf 'Старі релізи не видаляються автоматично.\n'
