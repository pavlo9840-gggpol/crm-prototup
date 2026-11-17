#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
INSTALL_ROOT="/opt/autozapaz"
VENV_DIR="$INSTALL_ROOT/venv"
DATA_DIR="/var/lib/autozapaz"
CONFIG_DIR="/etc/autozapaz"
ENV_FILE="$CONFIG_DIR/autozapaz.env"
SERVICE_FILE="/etc/systemd/system/autozapaz.service"
SERVICE_SOURCE="$APP_DIR/deploy/linux/autozapaz.service"
SERVICE_USER="autozapaz"

fail() { printf 'Помилка: %s\n' "$*" >&2; exit 1; }
info() { printf '\n==> %s\n' "$*"; }

[[ "$(id -u)" -eq 0 ]] || fail "Запустіть інсталятор від root: sudo bash deploy/linux/install.sh"
[[ -f "$APP_DIR/manage.py" && -f "$APP_DIR/requirements.txt" ]] || fail "Не знайдено manage.py або requirements.txt у $APP_DIR"
[[ -f "$SERVICE_SOURCE" ]] || fail "Не знайдено systemd unit: $SERVICE_SOURCE"
command -v python3 >/dev/null 2>&1 || fail "Потрібен Python 3."
command -v runuser >/dev/null 2>&1 || fail "Потрібна команда runuser (util-linux)."

if [[ ! -f "$ENV_FILE" ]]; then
    [[ -n "${AUTOZAPAZ_LICENSE_PUBLIC_KEY:-}" ]] || fail "Для першої інсталяції передайте AUTOZAPAZ_LICENSE_PUBLIC_KEY з вашого видавничого ключа."
    python3 - <<'PY' >/dev/null || fail "Потрібен Python 3 із модулем secrets."
import secrets
print(secrets.token_urlsafe(64))
PY
fi

info "Створення системного користувача та каталогів"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_DIR" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -o root -g "$SERVICE_USER" -m 0750 "$INSTALL_ROOT"
install -d -o root -g "$SERVICE_USER" -m 0750 "$APP_DIR"
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
        printf 'AUTOZAPAZ_REQUIRE_LICENSE=1\n'
        printf 'AUTOZAPAZ_SECURE_COOKIES=0\n'
    } > "$ENV_FILE"
    chown root:"$SERVICE_USER" "$ENV_FILE"
    chmod 0640 "$ENV_FILE"
else
    info "Наявний файл конфігурації збережено без змін: $ENV_FILE"
    grep -q '^DJANGO_SECRET_KEY=.' "$ENV_FILE" || fail "У $ENV_FILE відсутній DJANGO_SECRET_KEY."
    grep -q '^AUTOZAPAZ_LICENSE_PUBLIC_KEY=.' "$ENV_FILE" || fail "У $ENV_FILE не налаштовано AUTOZAPAZ_LICENSE_PUBLIC_KEY."
fi

info "Підготовка Python-середовища"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

info "Підготовка конфігурації Django"
# The env file is root-owned and mode 0640; only this trusted installer reads it.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
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

info "Встановлення systemd-служби"
install -o root -g root -m 0644 "$SERVICE_SOURCE" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable --now autozapaz.service
systemctl --no-pager --full status autozapaz.service || true

printf '\nАвтоЗапас встановлено як локальну службу. Адреса: http://127.0.0.1:8000/\n'
printf 'Конфігурація: %s\nДані: %s\nРезервні копії: %s/backups\n' "$ENV_FILE" "$DATA_DIR" "$DATA_DIR"
printf 'Керування: systemctl status|restart|stop autozapaz.service\n'
printf 'Журнал: journalctl -u autozapaz.service -f\n'
printf 'Не відкривайте порт 8000 у LAN: SQLite поки що підтримує лише локальну інсталяцію.\n'
