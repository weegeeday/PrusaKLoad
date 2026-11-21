#!/usr/bin/env bash
set -euo pipefail

KLIPPER_PATH="${KLIPPER_PATH:-$HOME/klipper}"
CONFIG_PATH="${CONFIG_PATH:-$HOME/printer_data/config}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODULES=("prusa_kload.py" "prusa_kload_material.py")

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Options:
  -k, --klipper-path PATH   Path to Klipper source directory (default: ~/klipper)
  -c, --config-path PATH    Path to printer_data config dir (default: ~/printer_data/config)
      --copy-configs        Copy starter config files into CONFIG_PATH if missing
      --uninstall           Remove PrusaKLoad symlinks from Klipper
  -h, --help                Show this help message
EOF
}

COPY_CONFIGS=0
UNINSTALL=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -k|--klipper-path)
            KLIPPER_PATH="$2"
            shift 2
            ;;
        -c|--config-path)
            CONFIG_PATH="$2"
            shift 2
            ;;
        --copy-configs)
            COPY_CONFIGS=1
            shift
            ;;
        --uninstall)
            UNINSTALL=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

EXTRAS_DIR="$KLIPPER_PATH/klippy/extras"
if [[ ! -d "$EXTRAS_DIR" ]]; then
    echo "Error: Klipper extras directory not found at '$EXTRAS_DIR'" >&2
    exit 1
fi

if [[ "$UNINSTALL" -eq 1 ]]; then
    for mod in "${MODULES[@]}"; do
        target="$EXTRAS_DIR/$mod"
        if [[ -L "$target" || -f "$target" ]]; then
            rm -f "$target"
            echo "Removed $target"
        fi
    done
else
    for mod in "${MODULES[@]}"; do
        src="$REPO_DIR/klippy/extras/$mod"
        target="$EXTRAS_DIR/$mod"
        ln -sfn "$src" "$target"
        echo "Linked $target -> $src"
    done

    if [[ "$COPY_CONFIGS" -eq 1 ]]; then
        mkdir -p "$CONFIG_PATH"
        for cfg in prusa_kload.cfg prusa_kload_menu.cfg; do
            dest="$CONFIG_PATH/$cfg"
            if [[ ! -e "$dest" ]]; then
                cp "$REPO_DIR/config/$cfg" "$dest"
                echo "Copied starter config to $dest"
            else
                echo "Config already exists, skipping: $dest"
            fi
        done
    fi
fi

if command -v systemctl >/dev/null 2>&1; then
    if systemctl list-unit-files | grep -q '^klipper\.service'; then
        echo "Restarting klipper service..."
        sudo systemctl restart klipper
    fi
fi

echo "Done."

