#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "========================================="
echo "  pCloud Upload Pipeline - Uninstall"
echo "========================================="
echo
echo "This will remove:"
echo "  - Dashboard container"
echo "  - Systemd watcher services"
echo "  - Scripts from /usr/local/bin/"
echo "  - Config from /etc/pcloud-pipeline.env"
echo
echo "Your pipeline data will NOT be deleted."
echo

read -rp "Proceed with uninstall? [y/N]: " CONFIRM
if [[ ! "$CONFIRM" =~ ^[Yy] ]]; then
    echo "Aborted."
    exit 0
fi

echo
echo "[1/4] Stopping dashboard..."
cd "$SCRIPT_DIR" 2>/dev/null && docker compose down 2>/dev/null || true

echo "[2/4] Stopping and removing systemd services..."
systemctl stop pcloud-staging-watcher pcloud-upload-watcher 2>/dev/null || true
systemctl disable pcloud-staging-watcher pcloud-upload-watcher 2>/dev/null || true
rm -f /etc/systemd/system/pcloud-staging-watcher.service
rm -f /etc/systemd/system/pcloud-upload-watcher.service
systemctl daemon-reload

echo "[3/4] Removing scripts..."
rm -f /usr/local/bin/pcloud-staging-watcher.sh
rm -f /usr/local/bin/pcloud-upload-watcher.sh

echo "[4/4] Removing config..."
rm -f /etc/pcloud-pipeline.env

echo
echo "Uninstall complete."
echo "Your pipeline data has been preserved."
echo "To remove data, delete the PIPELINE_BASE directory manually."
