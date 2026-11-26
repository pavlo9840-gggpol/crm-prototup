#!/usr/bin/env bash
set -Eeuo pipefail
INSTALL_ROOT="/opt/autozapaz"
APP_LINK="$INSTALL_ROOT/app"
VENV_LINK="$INSTALL_ROOT/venv"
RELEASES_DIR="$INSTALL_ROOT/releases"
VENVS_DIR="$INSTALL_ROOT/venvs"
DATA_DIR="/var/lib/autozapaz"
UPDATE_STATE="/var/lib/autozapaz-update-state.json"
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
exec 9>/run/lock/autozapaz-update.lock
flock -n 9 || fail "Інше оновлення вже виконується."
[[ "$VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || fail "Некоректний ідентифікатор версії."
[[ -d "$RELEASE_SOURCE" && -f "$RELEASE_SOURCE/manage.py" && -f "$RELEASE_SOURCE/requirements.txt" ]] || fail "Каталог релізу має містити manage.py та requirements.txt."
[[ -f "$ENV_FILE" && ! -L "$ENV_FILE" && -f "$APP_LINK/manage.py" && -x "$VENV_LINK/bin/python" ]] || fail "Не знайдено безпечну чинну інсталяцію АвтоЗапас."

[[ -d "$INSTALL_ROOT" && ! -L "$INSTALL_ROOT" ]] || fail "Кореневий каталог інсталяції відсутній або є символічним посиланням."
[[ "$(stat -c '%u' -- "$INSTALL_ROOT")" == "0" ]] || fail "Кореневий каталог інсталяції має належати root."
INSTALL_ROOT_MODE="$(stat -c '%a' -- "$INSTALL_ROOT")"
(( (8#$INSTALL_ROOT_MODE & 0022) == 0 )) || fail "Кореневий каталог інсталяції доступний для запису групі/іншим."
CURRENT_APP_REAL="$(readlink -f -- "$APP_LINK")"
CURRENT_VENV_REAL="$(readlink -f -- "$VENV_LINK")"
case "$CURRENT_APP_REAL/" in "$INSTALL_ROOT/"*) ;; *) fail "Чинний каталог програми знаходиться поза коренем інсталяції." ;; esac
case "$CURRENT_VENV_REAL/" in "$INSTALL_ROOT/"*) ;; *) fail "Чинне Python-середовище знаходиться поза коренем інсталяції." ;; esac
[[ -d "$CURRENT_APP_REAL" && -d "$CURRENT_VENV_REAL" ]] || fail "Шляхи чинної програми або Python-середовища некоректні."
[[ "$(find "$CURRENT_APP_REAL" -path "$CURRENT_APP_REAL/staticfiles" -prune -o \( -type f -o -type d \) \( ! -user root -o -perm /022 \) -print -quit)" == "" ]] || fail "Файли коду чинного релізу мають належати root і не бути доступними для запису групі/іншим."
[[ "$(find "$CURRENT_VENV_REAL" \( -type f -o -type d \) \( ! -user root -o -perm /022 \) -print -quit)" == "" ]] || fail "Файли чинного Python-середовища мають належати root і не бути доступними для запису групі/іншим."
[[ "$(stat -c '%u' -- "$ENV_FILE")" == "0" ]] || fail "Файл конфігурації має належати root."
ENV_MODE="$(stat -c '%a' -- "$ENV_FILE")"
(( (8#$ENV_MODE & 0022) == 0 )) || fail "Груповий або загальний запис у конфігурації заборонений."
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
: "${AUTOZAPAZ_RELEASE_PUBLIC_KEY:?У конфігурації немає AUTOZAPAZ_RELEASE_PUBLIC_KEY}"
[[ -x "$VENV_LINK/bin/python" ]] || fail "Не знайдено Python-середовище чинної інсталяції."
TRUSTED_VERIFIER="$APP_LINK/scripts/verify_release.py"
[[ -f "$TRUSTED_VERIFIER" && ! -L "$TRUSTED_VERIFIER" ]] || fail "Не знайдено verifier у чинній довіреній інсталяції."
[[ "$(stat -c '%u' -- "$TRUSTED_VERIFIER")" == "0" ]] || fail "Verifier чинної інсталяції має належати root."
VERIFIER_MODE="$(stat -c '%a' -- "$TRUSTED_VERIFIER")"
(( (8#$VERIFIER_MODE & 0022) == 0 )) || fail "Verifier не повинен бути доступним для запису групі/іншим."
CURRENT_RELEASE_SEQUENCE="$("$VENV_LINK/bin/python" "$TRUSTED_VERIFIER" --release "$CURRENT_APP_REAL" --public-key "$AUTOZAPAZ_RELEASE_PUBLIC_KEY" --print-sequence)" || fail "Не вдалося перевірити послідовність чинного підписаного релізу."
[[ "$CURRENT_RELEASE_SEQUENCE" =~ ^[0-9]+$ ]] || fail "Некоректна послідовність чинного релізу."
(( CURRENT_RELEASE_SEQUENCE < 9223372036854775806 )) || fail "Досягнуто максимальну послідовність релізів; потрібна процедура ротації лічильника."
MINIMUM_RELEASE_SEQUENCE="$((CURRENT_RELEASE_SEQUENCE + 1))"
"$VENV_LINK/bin/python" "$TRUSTED_VERIFIER" --release "$RELEASE_SOURCE" --public-key "$AUTOZAPAZ_RELEASE_PUBLIC_KEY" --version "$VERSION" --minimum-sequence "$MINIMUM_RELEASE_SEQUENCE" || fail "Підпис, контрольні суми або послідовність релізу не пройшли перевірку."
id "$SERVICE_USER" >/dev/null 2>&1 || fail "Не знайдено системного користувача $SERVICE_USER."
SERVICE_UID="$(id -u "$SERVICE_USER")"
[[ -d "$DATA_DIR" && ! -L "$DATA_DIR" ]] || fail "Постійний каталог даних відсутній або є символічним посиланням."
[[ ! -e "$UPDATE_STATE" && ! -L "$UPDATE_STATE" ]] || fail "Знайдено журнал незавершеного оновлення: $UPDATE_STATE. Повторний запуск заблоковано для захисту бази й резервної копії. Спочатку перевірте стан служби, посилання app/venv, .rollback-restore-* та виконайте ручне відновлення."
[[ -d "$DATA_DIR/media" && ! -L "$DATA_DIR/media" ]] || fail "Каталог медіафайлів відсутній або є символічним посиланням."
[[ -d "$BACKUP_DIR" && ! -L "$BACKUP_DIR" ]] || fail "Каталог резервних копій відсутній або є символічним посиланням."
for directory in "$DATA_DIR" "$DATA_DIR/media" "$BACKUP_DIR"; do
    [[ "$(stat -c '%u' -- "$directory")" == "$SERVICE_UID" ]] || fail "Каталог даних має належати користувачу $SERVICE_USER: $directory"
    DIRECTORY_MODE="$(stat -c '%a' -- "$directory")"
    (( (8#$DIRECTORY_MODE & 0077) == 0 )) || fail "Каталог даних не повинен бути доступним групі або іншим: $directory"
done
for database_file in "$DATA_DIR/db.sqlite3" "$DATA_DIR/db.sqlite3-wal" "$DATA_DIR/db.sqlite3-shm"; do
    [[ ! -L "$database_file" ]] || fail "Файл SQLite не може бути символічним посиланням: $database_file"
    if [[ -e "$database_file" ]]; then
        [[ -f "$database_file" ]] || fail "Файл SQLite має бути звичайним файлом: $database_file"
        [[ "$(stat -c '%u' -- "$database_file")" == "$SERVICE_UID" ]] || fail "Файл SQLite має належати користувачу $SERVICE_USER: $database_file"
        [[ "$(stat -c '%h' -- "$database_file")" == "1" ]] || fail "Файл SQLite не повинен мати додаткових hard links: $database_file"
        DATABASE_MODE="$(stat -c '%a' -- "$database_file")"
        (( (8#$DATABASE_MODE & 0022) == 0 )) || fail "Файл SQLite доступний для запису групі або іншим: $database_file"
    fi
done
systemctl cat "$SERVICE" >/dev/null 2>&1 || fail "Службу $SERVICE не встановлено."
case "$RELEASE_SOURCE/" in "$CURRENT_APP_REAL/"*) fail "Новий реліз не може бути всередині чинної програми." ;; esac
[[ "$(find "$RELEASE_SOURCE" -type l -print -quit)" == "" ]] || fail "Реліз містить символічні посилання."
[[ "$(find "$RELEASE_SOURCE" ! -type d ! -type f -print -quit)" == "" ]] || fail "Реліз містить спеціальні файли."

[[ "$AUTOZAPAZ_DATA_DIR" == "$DATA_DIR" ]] || fail "Оновлювач підтримує стандартний AUTOZAPAZ_DATA_DIR=$DATA_DIR."
BACKUP_DIR="$AUTOZAPAZ_BACKUP_DIR"
[[ "$BACKUP_DIR" == "$DATA_DIR/backups" ]] || fail "Оновлювач підтримує лише стандартний каталог резервних копій $DATA_DIR/backups."
[[ "$BACKUP_DIR" == /* ]] || fail "Каталог резервних копій має бути абсолютним шляхом."
[[ -d "$BACKUP_DIR" && ! -L "$BACKUP_DIR" ]] || fail "Каталог резервних копій відсутній або є символічним посиланням: $BACKUP_DIR"
: "${DJANGO_SECRET_KEY:?У конфігурації немає DJANGO_SECRET_KEY}"
: "${AUTOZAPAZ_DATA_DIR:?У конфігурації немає AUTOZAPAZ_DATA_DIR}"
: "${AUTOZAPAZ_BACKUP_DIR:?У конфігурації немає AUTOZAPAZ_BACKUP_DIR}"
: "${AUTOZAPAZ_LICENSE_PUBLIC_KEY:?У конфігурації немає AUTOZAPAZ_LICENSE_PUBLIC_KEY}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RELEASE_ID="${VERSION}-${STAMP}"
NEW_APP="$RELEASES_DIR/$RELEASE_ID"
NEW_VENV="$VENVS_DIR/$RELEASE_ID"
OLD_APP_REAL="$(readlink -f -- "$APP_LINK")"
OLD_VENV_REAL="$CURRENT_VENV_REAL"
OLD_APP_FALLBACK="$RELEASES_DIR/rollback-$STAMP"
OLD_VENV_FALLBACK="$VENVS_DIR/rollback-$STAMP"
RESTORE_DIR="$DATA_DIR/.rollback-restore-$STAMP"
BACKUP_PATH=""
APP_SWITCHED=0
VENV_SWITCHED=0
SERVICE_STOPPED=0
UPDATE_COMPLETE=0
ROLLBACK_ATTEMPTED=0
STATE_WRITTEN=0
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
write_update_state() {
    local phase="$1" temporary_state
    temporary_state="$(mktemp /var/lib/.autozapaz-update-state.XXXXXX)"
    python3 - "$temporary_state" "$phase" "$STAMP" "$OLD_APP_REAL" "$OLD_VENV_REAL" "$NEW_APP" "$NEW_VENV" "$RESTORE_DIR" "$BACKUP_PATH" <<'PY'
import json, os, sys
path, phase, stamp, old_app, old_venv, new_app, new_venv, restore_dir, backup = sys.argv[1:]
payload = {"format_version": 1, "phase": phase, "stamp": stamp, "old_app": old_app, "old_venv": old_venv, "new_app": new_app, "new_venv": new_venv, "restore_dir": restore_dir, "backup": backup}
with open(path, "w", encoding="utf-8") as stream:
    json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
PY
    chown root:root "$temporary_state"
    chmod 0600 "$temporary_state"
    mv -f -- "$temporary_state" "$UPDATE_STATE"
    python3 -c 'import os; fd=os.open("/var/lib", os.O_RDONLY | os.O_DIRECTORY); os.fsync(fd); os.close(fd)'
    STATE_WRITTEN=1
}
clear_update_state() {
    if [[ -e "$UPDATE_STATE" && ! -L "$UPDATE_STATE" && "$(stat -c '%u' -- "$UPDATE_STATE")" == "0" ]]; then
        rm -f -- "$UPDATE_STATE"
    fi
    STATE_WRITTEN=0
}
atomic_link() {
    local link_path="$1" target_path="$2" temporary_link="${1}.next-$STAMP"
    if ! ln -s "$target_path" "$temporary_link"; then
        return 1
    fi
    if [[ -L "$link_path" ]]; then
        if ! mv -Tf "$temporary_link" "$link_path"; then
            rm -f -- "$temporary_link"
            return 1
        fi
    else
        local fallback="$3"
        if [[ ! -e "$link_path" ]]; then
            rm -f -- "$temporary_link"
            fail "Не знайдено каталог для перемикання: $link_path"
        fi
        if [[ -e "$fallback" || -L "$fallback" ]]; then
            rm -f -- "$temporary_link"
            fail "Шлях резервного каталогу вже зайнятий: $fallback"
        fi
        if ! mv "$link_path" "$fallback"; then
            rm -f -- "$temporary_link"
            return 1
        fi
        if ! mv -Tf "$temporary_link" "$link_path"; then
            rm -f -- "$temporary_link"
            if ! mv "$fallback" "$link_path"; then
                ROLLBACK_ATTEMPTED=1
                printf 'КРИТИЧНО: не вдалося повернути каталог %s на місце %s. Служба залишена зупиненою; резервний каталог збережено.\n' "$fallback" "$link_path" >&2
            fi
            return 1
        fi
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
resume_service() {
    if ! systemctl start "$SERVICE"; then
        printf 'Не вдалося повторно запустити службу %s.\n' "$SERVICE" >&2
        return 1
    fi
    if ! health_check; then
        systemctl stop "$SERVICE" || true
        printf 'Стара служба %s не пройшла health-check після скасування оновлення.\n' "$SERVICE" >&2
        return 1
    fi
    SERVICE_STOPPED=0
    return 0
}
rollback_update() {
    local reason="$1"
    local temporary_db="$DATA_DIR/db.sqlite3.rollback-$STAMP"
    ROLLBACK_ATTEMPTED=1
    printf '\nПОМИЛКА: %s\nПовертаю попередню версію.\n' "$reason" >&2

    if ! systemctl stop "$SERVICE"; then
        printf 'Не вдалося зупинити службу; дані не змінювалися. Backup: %s\n' "$BACKUP_PATH" >&2
        return 1
    fi

    if [[ "$APP_SWITCHED" -eq 1 ]]; then
        if ! ln -s "$OLD_APP_REAL" "${APP_LINK}.rollback-$STAMP" ||
           ! mv -Tf "${APP_LINK}.rollback-$STAMP" "$APP_LINK"; then
            printf 'Не вдалося повернути попередній код. Служба залишена зупиненою. Backup: %s\n' "$BACKUP_PATH" >&2
            return 1
        fi
    fi
    if [[ "$VENV_SWITCHED" -eq 1 ]]; then
        if ! ln -s "$OLD_VENV_REAL" "${VENV_LINK}.rollback-$STAMP" ||
           ! mv -Tf "${VENV_LINK}.rollback-$STAMP" "$VENV_LINK"; then
            printf 'Не вдалося повернути попереднє Python-середовище. Служба залишена зупиненою. Backup: %s\n' "$BACKUP_PATH" >&2
            return 1
        fi
    fi

    if [[ "$APP_SWITCHED" -eq 1 || "$VENV_SWITCHED" -eq 1 ]]; then
        if [[ ! -d "$RESTORE_DIR" || ! -f "$RESTORE_DIR/db.sqlite3" ]]; then
            printf 'Каталог перевіреного відкату відсутній або не містить db.sqlite3. Служба залишена зупиненою. Backup: %s\n' "$BACKUP_PATH" >&2
            return 1
        fi

        for suffix in "" "-wal" "-shm"; do
            if [[ -e "$DATA_DIR/db.sqlite3$suffix" ]]; then
                if ! mv "$DATA_DIR/db.sqlite3$suffix" "$DATA_DIR/db.sqlite3$suffix.failed-$STAMP"; then
                    printf 'Не вдалося зберегти поточний файл db.sqlite3%s. Служба залишена зупиненою. Backup: %s\n' "$suffix" "$BACKUP_PATH" >&2
                    return 1
                fi
            fi
        done

        if ! cp "$RESTORE_DIR/db.sqlite3" "$temporary_db" ||
           ! chown "$SERVICE_USER:$SERVICE_USER" "$temporary_db" ||
           ! chmod 0600 "$temporary_db" ||
           ! mv "$temporary_db" "$DATA_DIR/db.sqlite3"; then
            rm -f -- "$temporary_db"
            printf 'Не вдалося відновити SQLite-базу. Служба залишена зупиненою. Backup: %s\n' "$BACKUP_PATH" >&2
            return 1
        fi

        if [[ -e "$DATA_DIR/media" ]]; then
            if ! mv "$DATA_DIR/media" "$DATA_DIR/media.failed-$STAMP"; then
                printf 'Не вдалося зберегти медіафайли невдалої версії. Служба залишена зупиненою. Backup: %s\n' "$BACKUP_PATH" >&2
                return 1
            fi
        fi
        if [[ -d "$RESTORE_DIR/media" ]]; then
            if ! mv "$RESTORE_DIR/media" "$DATA_DIR/media"; then
                printf 'Не вдалося повернути медіафайли з резервної копії. Служба залишена зупиненою. Backup: %s\n' "$BACKUP_PATH" >&2
                return 1
            fi
        elif ! install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$DATA_DIR/media"; then
            printf 'Не вдалося створити каталог медіафайлів. Служба залишена зупиненою. Backup: %s\n' "$BACKUP_PATH" >&2
            return 1
        fi
        if ! chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR/media" ||
           ! chmod 0700 "$DATA_DIR/media"; then
            printf 'Не вдалося встановити права на відновлені медіафайли. Служба залишена зупиненою. Backup: %s\n' "$BACKUP_PATH" >&2
            return 1
        fi
    fi

    if ! systemctl start "$SERVICE"; then
        printf 'Попередній код і дані повернуто, але служба не стартувала. Дивіться journalctl -u %s. Backup: %s\n' "$SERVICE" "$BACKUP_PATH" >&2
        return 1
    fi
    if ! health_check; then
        if ! systemctl stop "$SERVICE"; then
            printf 'Попередня версія не пройшла health-check, і службу не вдалося зупинити автоматично. Негайно перевірте systemd та journalctl -u %s. Backup: %s\n' "$SERVICE" "$BACKUP_PATH" >&2
        else
            printf 'Попередня версія не пройшла health-check; службу зупинено. Backup: %s\n' "$BACKUP_PATH" >&2
        fi
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
        if rollback_update "Неочікувана помилка (код $exit_code) під час оновлення."; then
            clear_update_state
        fi
    elif [[ "$SERVICE_STOPPED" -eq 1 ]]; then
        printf '\nНеочікувана помилка (код %s). Перевіряю відновлення чинної служби.\n' "$exit_code" >&2
        if resume_service; then
            clear_update_state
        else
            printf 'Не вдалося безпечно відновити службу; журнал стану збережено: %s\n' "$UPDATE_STATE" >&2
        fi
    else
        clear_update_state
    fi
    exit "$exit_code"
}
trap on_exit EXIT

info "Підготовка релізу $VERSION"
write_update_state "preparing_release"
cp -a "$RELEASE_SOURCE" "$NEW_APP"
chown -R "root:$SERVICE_USER" "$NEW_APP"
find "$NEW_APP" -type d -exec chmod 0750 {} +
find "$NEW_APP" -type f -exec chmod 0640 {} +
"$VENV_LINK/bin/python" "$TRUSTED_VERIFIER" --release "$NEW_APP" --public-key "$AUTOZAPAZ_RELEASE_PUBLIC_KEY" --version "$VERSION" --minimum-sequence "$MINIMUM_RELEASE_SEQUENCE" || fail "Staging-копія релізу не пройшла повторну перевірку послідовності та підпису."
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
write_update_state "service_stopped"
if ! BACKUP_OUTPUT="$(as_service "$OLD_VENV_REAL/bin/python" "$APP_LINK/manage.py" backup --keep 30 2>&1)"; then
    resume_service || true
    fail "Не вдалося створити резервну копію; оновлення скасовано. $BACKUP_OUTPUT"
fi
printf '%s\n' "$BACKUP_OUTPUT"
BACKUP_PATH="$(printf '%s\n' "$BACKUP_OUTPUT" | sed -n 's/^Saved //p' | tail -n 1)"
write_update_state "backup_created"
[[ -n "$BACKUP_PATH" && -f "$BACKUP_PATH" ]] || {
    resume_service || true
    fail "Команда backup не повернула шлях до архіву."
}
case "$(realpath -e -- "$BACKUP_PATH")" in
    "$(realpath -e -- "$BACKUP_DIR")"/*) ;;
    *) resume_service || true; fail "Backup створено поза каталогом резервних копій." ;;
esac
[[ -f "$BACKUP_PATH" && ! -L "$BACKUP_PATH" ]] || { resume_service || true; fail "Архів резервної копії має бути звичайним файлом без symlink." ; }
[[ "$(stat -c '%u' -- "$BACKUP_PATH")" == "$SERVICE_UID" && "$(stat -c '%h' -- "$BACKUP_PATH")" == "1" ]] || { resume_service || true; fail "Архів резервної копії має належати $SERVICE_USER і не мати hard links."; }
if ! chmod 0600 -- "$BACKUP_PATH"; then
    resume_service || true
    fail "Не вдалося обмежити доступ до архіву резервної копії."
fi
if ! as_service "$OLD_VENV_REAL/bin/python" "$APP_LINK/manage.py" verify_backup "$BACKUP_PATH"; then
    resume_service || true
    fail "Перевірка резервної копії не пройшла; оновлення скасовано."
fi
if ! as_service "$OLD_VENV_REAL/bin/python" "$APP_LINK/manage.py" restore_backup "$BACKUP_PATH" "$RESTORE_DIR" --apply; then
    resume_service || true
    fail "Не вдалося підготувати каталог відкату; оновлення скасовано."
fi
write_update_state "rollback_ready"

info "Перемикання коду та Python-середовища"
atomic_link "$APP_LINK" "$NEW_APP" "$OLD_APP_FALLBACK"
if [[ "$OLD_APP_REAL" == "$APP_LINK" ]]; then OLD_APP_REAL="$OLD_APP_FALLBACK"; fi
APP_SWITCHED=1
write_update_state "app_switched"
atomic_link "$VENV_LINK" "$NEW_VENV" "$OLD_VENV_FALLBACK"
if [[ "$OLD_VENV_REAL" == "$VENV_LINK" ]]; then OLD_VENV_REAL="$OLD_VENV_FALLBACK"; fi
VENV_SWITCHED=1
write_update_state "venv_switched"

info "Застосування міграцій"
write_update_state "migrations_started"
if ! as_service "$VENV_LINK/bin/python" "$APP_LINK/manage.py" migrate --noinput; then
    if rollback_update "Міграції завершилися помилкою."; then clear_update_state; fi
    fail "Оновлення скасовано."
fi
if ! as_service "$VENV_LINK/bin/python" "$APP_LINK/manage.py" check --database default; then
    if rollback_update "Django system check не пройшов."; then clear_update_state; fi
    fail "Оновлення скасовано."
fi
if ! as_service "$VENV_LINK/bin/python" "$APP_LINK/manage.py" collectstatic --noinput; then
    if rollback_update "collectstatic завершився помилкою."; then clear_update_state; fi
    fail "Оновлення скасовано."
fi

info "Запуск і health-check"
if ! systemctl start "$SERVICE" || ! health_check; then
    if rollback_update "Нова версія не запустилася або не відповідає на HTTP."; then clear_update_state; fi
    fail "Оновлення скасовано."
fi
SERVICE_STOPPED=0
write_update_state "health_check_passed"
# Mark the release committed and remove the fail-closed journal before
# deleting rollback material. A crash during cleanup then leaves only an
# orphaned restore directory, never a journal that points to missing data.
UPDATE_COMPLETE=1
clear_update_state
rm -rf -- "$RESTORE_DIR"
printf '\nОновлення до %s успішне.\n' "$VERSION"
printf 'Backup перед оновленням: %s\n' "$BACKUP_PATH"
printf 'Попередній код і venv збережені: %s ; %s\n' "$OLD_APP_REAL" "$OLD_VENV_REAL"
printf 'Старі релізи не видаляються автоматично.\n'
