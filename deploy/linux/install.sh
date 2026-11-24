#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="/opt/autozapaz/app"
INSTALL_ROOT="/opt/autozapaz"
VENV_DIR="$INSTALL_ROOT/venv"
DATA_DIR="/var/lib/autozapaz"
CONFIG_DIR="/etc/autozapaz"
ENV_FILE="$CONFIG_DIR/autozapaz.env"
SERVICE_FILE="/etc/systemd/system/autozapaz.service"
SERVICE_SOURCE="$APP_DIR/deploy/linux/autozapaz.service"
SERVICE_USER="autozapaz"

UNIT_INSTALLED=0
SERVICE_STARTED=0
INSTALL_COMPLETE=0

on_exit() {
    local exit_code="$?"
    trap - EXIT
    if [[ "$INSTALL_COMPLETE" -eq 1 || "$exit_code" -eq 0 ]]; then
        exit "$exit_code"
    fi
    printf '\nІнсталяція завершилася помилкою (код %s). Виконую безпечне очищення служби.\n' "$exit_code" >&2
    if [[ "$SERVICE_STARTED" -eq 1 ]]; then
        systemctl stop autozapaz.service >/dev/null 2>&1 || true
    fi
    if [[ "$UNIT_INSTALLED" -eq 1 ]]; then
        systemctl disable autozapaz.service >/dev/null 2>&1 || true
        rm -f -- "$SERVICE_FILE"
        systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    printf 'Каталог програми, конфігурація та дані збережені для діагностики/повторного запуску.\n' >&2
    exit "$exit_code"
}
trap on_exit EXIT

fail() { printf 'Помилка: %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> %s\n' "$*"; }

[[ "$(id -u)" -eq 0 ]] || fail "Запустіть інсталятор від root: sudo bash deploy/linux/install.sh"
[[ -f "$APP_DIR/manage.py" && -f "$APP_DIR/requirements.txt" ]] || fail "Спочатку розпакуйте реліз у $APP_DIR."
[[ -f "$SERVICE_SOURCE" ]] || fail "Не знайдено systemd unit: $SERVICE_SOURCE"
command -v python3 >/dev/null 2>&1 || fail "Потрібен Python 3."
command -v runuser >/dev/null 2>&1 || fail "Потрібна команда runuser (util-linux)."
[[ ! -e "$SERVICE_FILE" ]] || fail "Служба вже встановлена. Для неї використовуйте update.sh."

validate_env_file() {
    local owner mode
    [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "Конфігурація має бути звичайним файлом без символічного посилання: $ENV_FILE"
    owner="$(stat -c '%u' -- "$ENV_FILE")"
    mode="$(stat -c '%a' -- "$ENV_FILE")"
    [[ "$owner" == "0" ]] || fail "Власником $ENV_FILE має бути root."
    (( (8#$mode & 0022) == 0 )) || fail "Груповий або загальний запис у $ENV_FILE заборонений."
}

if [[ -e "$ENV_FILE" ]]; then
    validate_env_file
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi
[[ "${AUTOZAPAZ_RELEASE_PUBLIC_KEY:-}" =~ ^[A-Za-z0-9_-]{43}$ ]] || fail "Потрібен AUTOZAPAZ_RELEASE_PUBLIC_KEY (Ed25519 Base64URL, 43 символи)."
TRUSTED_VERIFIER="/usr/local/lib/autozapaz/verify_release.py"
[[ -f "$TRUSTED_VERIFIER" && ! -L "$TRUSTED_VERIFIER" ]] || fail "Не знайдено окремий довірений bootstrap verifier: $TRUSTED_VERIFIER"
[[ "$(stat -c '%u' -- "$TRUSTED_VERIFIER")" == "0" ]] || fail "Bootstrap verifier має належати root."
VERIFIER_MODE="$(stat -c '%a' -- "$TRUSTED_VERIFIER")"
(( (8#$VERIFIER_MODE & 0022) == 0 )) || fail "Bootstrap verifier не повинен бути доступним для запису групі/іншим."
python3 -c 'import cryptography' >/dev/null 2>&1 || fail "Системному Python потрібен пакет python3-cryptography."
python3 "$TRUSTED_VERIFIER" --release "$APP_DIR" --public-key "$AUTOZAPAZ_RELEASE_PUBLIC_KEY" || fail "Підпис або контрольні суми початкового релізу не пройшли перевірку."

info "Створення системного користувача та каталогів"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_DIR" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -o root -g "$SERVICE_USER" -m 0750 "$INSTALL_ROOT"
chown root:"$SERVICE_USER" "$APP_DIR"
chmod 0750 "$APP_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$DATA_DIR" "$DATA_DIR/media" "$DATA_DIR/backups"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$APP_DIR/staticfiles"
install -d -o root -g "$SERVICE_USER" -m 0750 "$CONFIG_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
    SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')"
    umask 027
    {
        printf 'DJANGO_SECRET_KEY=%s\n' "$SECRET_KEY"
        printf 'DJANGO_DEBUG=0\n'
        printf 'DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,[::1]\n'
        printf 'DJANGO_CSRF_TRUSTED_ORIGINS=\n'
        printf 'AUTOZAPAZ_DATA_DIR=%s\n' "$DATA_DIR"
        printf 'AUTOZAPAZ_BACKUP_DIR=%s/backups\n' "$DATA_DIR"
        printf 'AUTOZAPAZ_LICENSE_PUBLIC_KEY=%s\n' "$AUTOZAPAZ_LICENSE_PUBLIC_KEY"
        printf 'AUTOZAPAZ_RELEASE_PUBLIC_KEY=%s\n' "$AUTOZAPAZ_RELEASE_PUBLIC_KEY"
        printf 'AUTOZAPAZ_REQUIRE_LICENSE=1\n'
        printf 'AUTOZAPAZ_SECURE_COOKIES=0\n'
    } > "$ENV_FILE"
    chown root:"$SERVICE_USER" "$ENV_FILE"
    chmod 0640 "$ENV_FILE"
else
    validate_env_file
    info "Наявний файл конфігурації збережено без змін: $ENV_FILE"
    grep -q '^DJANGO_SECRET_KEY=.' "$ENV_FILE" || fail "У $ENV_FILE відсутній DJANGO_SECRET_KEY."
    grep -q '^AUTOZAPAZ_LICENSE_PUBLIC_KEY=.' "$ENV_FILE" || fail "У $ENV_FILE не налаштовано AUTOZAPAZ_LICENSE_PUBLIC_KEY."
    grep -q '^AUTOZAPAZ_RELEASE_PUBLIC_KEY=.' "$ENV_FILE" || fail "У $ENV_FILE не налаштовано AUTOZAPAZ_RELEASE_PUBLIC_KEY."
    PUBLIC_KEY_VALUE="$(sed -n 's/^AUTOZAPAZ_LICENSE_PUBLIC_KEY=//p' "$ENV_FILE")"
    RELEASE_KEY_VALUE="$(sed -n 's/^AUTOZAPAZ_RELEASE_PUBLIC_KEY=//p' "$ENV_FILE")"
    [[ "$PUBLIC_KEY_VALUE" =~ ^[A-Za-z0-9_-]{43}$ ]] || fail "У $ENV_FILE некоректний відкритий Ed25519-ключ ліцензії."
    [[ "$RELEASE_KEY_VALUE" == "$AUTOZAPAZ_RELEASE_PUBLIC_KEY" ]] || fail "Ключ підпису релізів у середовищі та конфігурації не збігається."
fi

info "Підготовка Python-середовища"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

info "Підготовка конфігурації Django"
# Configuration was validated and loaded before any package installation or migration.

# Refuse unsafe or non-standard production configuration before migrations.
[[ "${DJANGO_DEBUG:-}" == "0" ]] || fail "Для встановлення DEBUG має бути 0."
[[ "${DJANGO_ALLOWED_HOSTS:-}" =~ [^[:space:]] ]] || fail "DJANGO_ALLOWED_HOSTS не може бути порожнім."
[[ "${AUTOZAPAZ_DATA_DIR:-}" == "$DATA_DIR" ]] || fail "AUTOZAPAZ_DATA_DIR має бути $DATA_DIR."
[[ "${AUTOZAPAZ_BACKUP_DIR:-}" == "$DATA_DIR/backups" ]] || fail "AUTOZAPAZ_BACKUP_DIR має бути $DATA_DIR/backups."
[[ "${AUTOZAPAZ_REQUIRE_LICENSE:-}" == "1" ]] || fail "AUTOZAPAZ_REQUIRE_LICENSE має бути 1."
[[ "${AUTOZAPAZ_LICENSE_PUBLIC_KEY:-}" =~ ^[A-Za-z0-9_-]{43}$ ]] || fail "Некоректний відкритий Ed25519-ключ ліцензії."
export DJANGO_SETTINGS_MODULE=config.settings
runuser -u "$SERVICE_USER" -- env \
    DJANGO_SECRET_KEY="$DJANGO_SECRET_KEY" \
    DJANGO_DEBUG="$DJANGO_DEBUG" \
    DJANGO_ALLOWED_HOSTS="$DJANGO_ALLOWED_HOSTS" \
    DJANGO_CSRF_TRUSTED_ORIGINS="$DJANGO_CSRF_TRUSTED_ORIGINS" \
    AUTOZAPAZ_DATA_DIR="$AUTOZAPAZ_DATA_DIR" \
    AUTOZAPAZ_BACKUP_DIR="$AUTOZAPAZ_BACKUP_DIR" \
    AUTOZAPAZ_LICENSE_PUBLIC_KEY="$AUTOZAPAZ_LICENSE_PUBLIC_KEY" \
    AUTOZAPAZ_REQUIRE_LICENSE="$AUTOZAPAZ_REQUIRE_LICENSE" \
    AUTOZAPAZ_SECURE_COOKIES="$AUTOZAPAZ_SECURE_COOKIES" \
    "$VENV_DIR/bin/python" "$APP_DIR/manage.py" migrate --noinput

runuser -u "$SERVICE_USER" -- env \
    DJANGO_SECRET_KEY="$DJANGO_SECRET_KEY" DJANGO_DEBUG="$DJANGO_DEBUG" \
    DJANGO_ALLOWED_HOSTS="$DJANGO_ALLOWED_HOSTS" \
    AUTOZAPAZ_DATA_DIR="$AUTOZAPAZ_DATA_DIR" \
    AUTOZAPAZ_BACKUP_DIR="$AUTOZAPAZ_BACKUP_DIR" \
    AUTOZAPAZ_LICENSE_PUBLIC_KEY="$AUTOZAPAZ_LICENSE_PUBLIC_KEY" \
    AUTOZAPAZ_REQUIRE_LICENSE="$AUTOZAPAZ_REQUIRE_LICENSE" \
    "$VENV_DIR/bin/python" "$APP_DIR/manage.py" collectstatic --noinput

health_check() {
    local attempt
    for attempt in $(seq 1 30); do
        if systemctl is-active --quiet autozapaz.service && python3 - "http://127.0.0.1:8000/login/" <<'PY'
import sys
import urllib.error
import urllib.request

try:
    request = urllib.request.Request(sys.argv[1], headers={"User-Agent": "AutoZapaz-Installer/1"})
    with urllib.request.urlopen(request, timeout=3) as response:
        if 200 <= response.status < 400:
            raise SystemExit(0)
except (urllib.error.URLError, TimeoutError, OSError):
    pass
raise SystemExit(1)
PY
        then
            return 0
        fi
        sleep 2
    done
    return 1
}

info "Встановлення systemd-служби"
install -o root -g root -m 0644 "$SERVICE_SOURCE" "$SERVICE_FILE"
UNIT_INSTALLED=1
systemctl daemon-reload
SERVICE_STARTED=1
systemctl enable --now autozapaz.service
if ! health_check; then
    systemctl stop autozapaz.service || true
    fail "Служба не пройшла HTTP health-check. Перевірте журнал: journalctl -u autozapaz.service -n 100"
fi
systemctl --no-pager --full status autozapaz.service || true
INSTALL_COMPLETE=1

printf '\nАвтоЗапас встановлено як локальну службу. Адреса: http://127.0.0.1:8000/\n'
printf 'Конфігурація: %s\nДані: %s\nРезервні копії: %s/backups\n' "$ENV_FILE" "$DATA_DIR" "$DATA_DIR"
printf 'Керування: systemctl status|restart|stop autozapaz.service\n'
printf 'Журнал: journalctl -u autozapaz.service -f\n'
printf 'Не відкривайте порт 8000 у LAN: SQLite поки що підтримує лише локальну інсталяцію.\n'
