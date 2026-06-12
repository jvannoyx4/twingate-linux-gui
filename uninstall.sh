#!/usr/bin/env bash
set -euo pipefail

APP_NAME="twingate-linux-gui"
APP_ID="twingate-gui"
PREFIX="${PREFIX:-${HOME}/.local}"

rm -f "${PREFIX}/bin/twingate-gui"
rm -f "${PREFIX}/share/applications/${APP_ID}.desktop"
rm -f "${PREFIX}/share/icons/hicolor/scalable/apps/${APP_ID}.svg"
rm -rf "${PREFIX}/opt/${APP_NAME}"

if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "${PREFIX}/share/icons/hicolor" >/dev/null 2>&1 || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${PREFIX}/share/applications" >/dev/null 2>&1 || true
fi

echo "Uninstalled Twingate Linux GUI."
