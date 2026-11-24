#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_ROOT="/opt/autozapaz"
DATA_DIR="/var/lib/autozapaz"
CONFIG_DIR="/etc/autozapaz"
SERVICE_FILE="/etc/systemd/system/autozapaz.service"
TRUSTED_VERIFIER_DIR="/usr/local/lib/autozapaz"
SERVICE="autozapaz.service"
SERVICE_USER="autozapaz"
PURGE_DATA=0

fail() { printf 'Помилка видалення АвтоЗапас: %s\n' "$*" >&2; exit 1; }

if [[ $# -gt 1 ]]; then fail "Використання: uninstall.sh [--purge-data]"; fi
if [[ $# -eq 1 ]]; then
    [[ "$1" == "--purge-data" ]] || fail "Невідомий параметр: $1"
    PURGE_DATA=1
fi
[[ "$(id -u)" -eq 0 ]] || fail "Запустіть від root: sudo bash deploy/linux/uninstall.sh"

# Refuse redirected paths before any service or filesystem mutation.
for directory in "$INSTALL_ROOT" "$TRUSTED_VERIFIER_DIR" "$DATA_DIR" "$CONFIG_DIR"; do
    if [[ -L "$directory" ]]; then fail "Каталог $directory є символічним посиланням; автоматичне видалення скасовано."; fi
    if [[ -e "$directory" && ! -d "$directory" ]]; then fail "Шлях $directory не є каталогом."; fi
done
if [[ -L "$SERVICE_FILE" ]]; then fail "Файл systemd unit є символічним посиланням; автоматичне видалення скасовано."; fi

if systemctl cat "$SERVICE" >/dev/null 2>&1; then
    systemctl stop "$SERVICE" || fail "Не вдалося зупинити службу; файли не видалялися."
    systemctl disable "$SERVICE" || fail "Не вдалося вимкнути службу; файли не видалялися."
fi
if [[ -e "$SERVICE_FILE" ]]; then rm -f -- "$SERVICE_FILE"; systemctl daemon-reload; fi
if [[ -d "$INSTALL_ROOT" ]]; then rm -rf --one-file-system -- "$INSTALL_ROOT"; fi
if [[ -d "$TRUSTED_VERIFIER_DIR" ]]; then rm -rf --one-file-system -- "$TRUSTED_VERIFIER_DIR"; fi

if [[ "$PURGE_DATA" -eq 1 ]]; then
    if [[ -d "$DATA_DIR" ]]; then rm -rf --one-file-system -- "$DATA_DIR"; fi
    if [[ -d "$CONFIG_DIR" ]]; then rm -rf --one-file-system -- "$CONFIG_DIR"; fi
    if id "$SERVICE_USER" >/dev/null 2>&1; then userdel "$SERVICE_USER" || fail "Програму видалено, але системного користувача $SERVICE_USER не вдалося видалити."; fi
    printf '\nАвтоЗапас видалено разом із конфігурацією, базою, медіафайлами та резервними копіями.\n'
else
    printf '\nПрограму АвтоЗапас та systemd-службу видалено.\n'
    printf 'Дані, конфігурація та системний користувач збережені:\n  %s\n  %s\n' "$DATA_DIR" "$CONFIG_DIR"
    printf 'Для повного видалення даних повторіть команду з --purge-data.\n'
fi
