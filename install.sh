#!/usr/bin/env bash
set -euo pipefail

APP_NAME="twingate-linux-gui"
APP_ID="twingate-gui"
PREFIX="${PREFIX:-${HOME}/.local}"
APP_DIR="${PREFIX}/opt/${APP_NAME}"
BIN_DIR="${PREFIX}/bin"
APPLICATIONS_DIR="${PREFIX}/share/applications"
ICON_DIR="${PREFIX}/share/icons/hicolor/scalable/apps"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-/usr/bin/python3}"

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

missing=()
for command in twingate tar; do
  if ! command_exists "${command}"; then
    missing+=("${command}")
  fi
done
if [ ! -x "${PYTHON}" ] && ! command_exists "${PYTHON}"; then
  missing+=("${PYTHON}")
fi

if [ "${#missing[@]}" -gt 0 ]; then
  printf 'Missing required command(s): %s\n' "${missing[*]}" >&2
  printf 'Install the official Twingate Linux client and Python 3 before installing this GUI.\n' >&2
  exit 1
fi

if ! env \
  -u SNAP \
  -u SNAP_NAME \
  -u SNAP_INSTANCE_NAME \
  -u SNAP_REVISION \
  -u SNAP_ARCH \
  -u SNAP_COOKIE \
  -u SNAP_CONTEXT \
  -u SNAP_DATA \
  -u SNAP_COMMON \
  -u SNAP_USER_DATA \
  -u SNAP_USER_COMMON \
  -u SNAP_LIBRARY_PATH \
  -u GTK_EXE_PREFIX \
  -u GTK_IM_MODULE_FILE \
  -u GTK_PATH \
  "${PYTHON}" - <<'PY' >/dev/null 2>&1
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("AppIndicator3", "0.1")
gi.require_version("Notify", "0.7")
PY
then
  cat >&2 <<'EOF'
Missing Python GTK/AppIndicator bindings.

On Ubuntu/Debian, install:
  sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-appindicator3-0.1 gir1.2-notify-0.7
EOF
  exit 1
fi

mkdir -p "${APP_DIR}" "${BIN_DIR}" "${APPLICATIONS_DIR}" "${ICON_DIR}"

rm -rf "${APP_DIR}"
mkdir -p "${APP_DIR}"
tar -C "${SOURCE_DIR}" \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.log' \
  --exclude '.env' \
  --exclude '.env.*' \
  --exclude 'dist' \
  -cf - . | tar -C "${APP_DIR}" -xf -

chmod +x "${APP_DIR}/twingate-gui" "${APP_DIR}/twingate-gui.py" "${APP_DIR}/twingate-chrome-profile-picker.py"
ln -sfn "${APP_DIR}/twingate-gui" "${BIN_DIR}/twingate-gui"

cp "${APP_DIR}/assets/twingate-tray-online.svg" "${ICON_DIR}/${APP_ID}.svg"
sed "s|^Exec=.*|Exec=${BIN_DIR}/twingate-gui|" "${APP_DIR}/twingate-gui.desktop" > "${APPLICATIONS_DIR}/${APP_ID}.desktop"

if command_exists gtk-update-icon-cache; then
  gtk-update-icon-cache -f -t "${PREFIX}/share/icons/hicolor" >/dev/null 2>&1 || true
fi
if command_exists update-desktop-database; then
  update-desktop-database "${APPLICATIONS_DIR}" >/dev/null 2>&1 || true
fi

cat <<EOF
Installed Twingate Linux GUI.

App files: ${APP_DIR}
Launcher:  ${APPLICATIONS_DIR}/${APP_ID}.desktop
Command:   ${BIN_DIR}/twingate-gui

Run it from your app launcher or with:
  ${BIN_DIR}/twingate-gui
EOF
