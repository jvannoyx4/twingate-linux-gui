#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
VERSION="${VERSION:-$(date +%Y.%m.%d)}"
PACKAGE="twingate-linux-gui-${VERSION}"

mkdir -p "${DIST_DIR}"
rm -rf "${DIST_DIR:?}/${PACKAGE}" "${DIST_DIR}/${PACKAGE}.tar.gz"
mkdir -p "${DIST_DIR}/${PACKAGE}"

tar -C "${ROOT_DIR}" \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.log' \
  --exclude 'dist' \
  -cf - . | tar -C "${DIST_DIR}/${PACKAGE}" -xf -

tar -C "${DIST_DIR}" -czf "${DIST_DIR}/${PACKAGE}.tar.gz" "${PACKAGE}"
rm -rf "${DIST_DIR}/${PACKAGE}"

echo "${DIST_DIR}/${PACKAGE}.tar.gz"
