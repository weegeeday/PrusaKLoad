#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULES=("prusa_kload.py" "prusa_kload_material.py")
CONFIG_FILES=("prusa_kload.cfg" "prusa_kload_menu.cfg")

# Default paths (auto-detect legacy ~/klipper_config if ~/printer_data/config is absent)
KLIPPER_PATH="${KLIPPER_PATH:-$HOME/klipper}"
if [[ -n "${CONFIG_PATH:-}" ]]; then
    CONFIG_PATH="$CONFIG_PATH"
elif [[ -d "$HOME/printer_data/config" || ! -d "$HOME/klipper_config" ]]; then
    CONFIG_PATH="$HOME/printer_data/config"
else
    CONFIG_PATH="$HOME/klipper_config"
fi
KLIPPER_SERVICE="${KLIPPER_SERVICE:-klipper}"
MOONRAKER_SERVICE="${MOONRAKER_SERVICE:-moonraker}"

COPY_CONFIGS=0
ADD_UPDATE_MANAGER=0
UNINSTALL=0
NO_RESTART=0
EXPLICIT_KLIPPER_PATH=0

# Colors (only when writing to an interactive terminal)
if [[ -t 1 ]]; then
    C_RESET="\033[0m"
    C_BOLD="\033[1m"
    C_ORANGE="\033[38;5;208m"
    C_GREEN="\033[32m"
    C_YELLOW="\033[33m"
    C_RED="\033[31m"
    C_CYAN="\033[36m"
else
    C_RESET=""
    C_BOLD=""
    C_ORANGE=""
    C_GREEN=""
    C_YELLOW=""
    C_RED=""
    C_CYAN=""
fi

print_banner() {
    printf "%b" "$C_ORANGE"
    cat <<'EOF'
                                                              @@@@
                                                     @@@     @@@@@
                                                    @@@@   @@@@@        @@                                         @@
                                                    @@@@@ @@@@@         @@                                         @@
@@@@@@@@  @@@@@@@@ @@@    @@  @@@@@@@    @@@         @@@@@@@@@          @@           @@@@@       @@@@@       @@@@@ @@
@@@   @@@ @@    @@ @@@    @@ @@@        @@@@@        @@@@@@@@           @@         @@@@@@@@@   @@@@  @@@   @@@@  @@@@
@@@@@@@@  @@@@@@@  @@@    @@   @@@@@@   @@ @@@       @@@@@@@@@@         @@        @@@@@@@@@@@ @@@     @@@  @@      @@
@@@       @@   @@@  @@    @@       @@@ @@@@@@@@       @@@@@@@@@@@@      @@        @@@@@ @@@@@ @@@     @@@  @@      @@
@@@       @@    @@  @@@@@@@@  @@@@@@@ @@@    @@       @@@@@  @@@@@@@@   @@@@@@@@@  @@@@@@@@@   @@@@  @@@@  @@@@  @@@@
                                                       @@@@     @@@@@   @@@@@@@@@@   @@@@@@     @@@@@@@@@    @@@@@@
                                                       @@@@
EOF
    printf "%b\n" "$C_RESET"
}

info()    { printf "%b[info]%b %s\n" "$C_CYAN" "$C_RESET" "$*"; }
ok()      { printf "%b[ok]%b   %s\n" "$C_GREEN" "$C_RESET" "$*"; }
warn()    { printf "%b[warn]%b %s\n" "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()     { printf "%b[err]%b  %s\n" "$C_RED" "$C_RESET" "$*" >&2; }

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Options:
  -k, --klipper-path PATH    Path to Klipper source directory (default: ~/klipper)
  -c, --config-path PATH     Path to printer config directory (default: ~/printer_data/config)
  -s, --service NAME         Klipper systemd service name (default: klipper)
      --copy-configs         Copy starter config files into CONFIG_PATH if missing
      --add-update-manager   Add [update_manager prusa_kload] to moonraker.conf if missing
      --no-restart           Skip restarting Klipper/Moonraker systemd services
      --uninstall            Remove PrusaKLoad symlinks and cached bytecode from Klipper
  -h, --help                 Show this help message
EOF
}

require_arg() {
    if [[ $# -lt 2 || -z "$2" ]]; then
        err "Option '$1' requires a value."
        usage >&2
        exit 1
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -k|--klipper-path)
            require_arg "$@"
            KLIPPER_PATH="$2"
            EXPLICIT_KLIPPER_PATH=1
            shift 2
            ;;
        -c|--config-path)
            require_arg "$@"
            CONFIG_PATH="$2"
            shift 2
            ;;
        -s|--service)
            require_arg "$@"
            KLIPPER_SERVICE="$2"
            shift 2
            ;;
        --copy-configs)
            COPY_CONFIGS=1
            shift
            ;;
        --add-update-manager)
            ADD_UPDATE_MANAGER=1
            shift
            ;;
        --no-restart)
            NO_RESTART=1
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
            err "Unknown option: $1"
            usage >&2
            exit 1
            ;;
    esac
done

# Guard against accidental `sudo ./install.sh` on a real host (while allowing Docker/CI with -k)
if [[ "${EUID:-$(id -u)}" -eq 0 && "$EXPLICIT_KLIPPER_PATH" -eq 0 ]]; then
    err "Do not run this script with sudo. Run as your normal Klipper user (e.g. ./install.sh)."
    err "If installing inside a container as root, pass -k <klipper-path> explicitly."
    exit 1
fi

EXTRAS_DIR="$KLIPPER_PATH/klippy/extras"
if [[ ! -d "$EXTRAS_DIR" ]]; then
    err "Klipper extras directory not found at '$EXTRAS_DIR'."
    err "Pass the correct path with: $(basename "$0") -k /path/to/klipper"
    exit 1
fi

clean_bytecode() {
    local pycache="$EXTRAS_DIR/__pycache__"
    if [[ -d "$pycache" ]]; then
        for mod in "${MODULES[@]}"; do
            local stem="${mod%.py}"
            rm -f "$pycache/${stem}"*.pyc
        done
    fi
}

restart_service() {
    local svc="$1"
    if [[ "$NO_RESTART" -eq 1 ]]; then
        return
    fi
    if ! command -v systemctl >/dev/null 2>&1; then
        return
    fi
    if systemctl list-unit-files "${svc}.service" 2>/dev/null | grep -q "^${svc}\.service"; then
        info "Restarting ${svc}.service..."
        if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
            systemctl restart "$svc"
        else
            sudo systemctl restart "$svc"
        fi
        ok "Restarted ${svc}.service"
    fi
}

MOONRAKER_UPDATED=0

print_banner

if [[ "$UNINSTALL" -eq 1 ]]; then
    info "Uninstalling PrusaKLoad from $EXTRAS_DIR"
    for mod in "${MODULES[@]}"; do
        target="$EXTRAS_DIR/$mod"
        if [[ -L "$target" || -f "$target" ]]; then
            rm -f "$target"
            ok "Removed $target"
        else
            info "Not present, skipping: $target"
        fi
    done
    clean_bytecode
    restart_service "$KLIPPER_SERVICE"
    echo
    ok "PrusaKLoad uninstalled."
    info "Your config files in '$CONFIG_PATH' were left untouched."
    info "Remember to remove '[include prusa_kload.cfg]' (and 'prusa_kload_menu.cfg') from printer.cfg."
    exit 0
fi

info "Installing PrusaKLoad into $EXTRAS_DIR"
for mod in "${MODULES[@]}"; do
    src="$REPO_DIR/klippy/extras/$mod"
    target="$EXTRAS_DIR/$mod"
    if [[ ! -f "$src" ]]; then
        err "Source module missing: $src"
        exit 1
    fi
    ln -sfn "$src" "$target"
    ok "Linked $target -> $src"
done
clean_bytecode

if [[ "$COPY_CONFIGS" -eq 1 ]]; then
    mkdir -p "$CONFIG_PATH"
    for cfg in "${CONFIG_FILES[@]}"; do
        dest="$CONFIG_PATH/$cfg"
        if [[ ! -e "$dest" ]]; then
            cp "$REPO_DIR/config/$cfg" "$dest"
            ok "Copied starter config to $dest"
        else
            info "Config already exists, skipping: $dest"
        fi
    done
fi

if [[ "$ADD_UPDATE_MANAGER" -eq 1 ]]; then
    MOONRAKER_CONF="$CONFIG_PATH/moonraker.conf"
    if [[ ! -f "$MOONRAKER_CONF" ]]; then
        warn "moonraker.conf not found at '$MOONRAKER_CONF'; skipping update_manager setup."
    elif grep -Eq '^\s*\[update_manager\s+prusa_kload\]' "$MOONRAKER_CONF"; then
        info "[update_manager prusa_kload] already present in $MOONRAKER_CONF"
    else
        cat >> "$MOONRAKER_CONF" <<EOF

[update_manager prusa_kload]
type: git_repo
path: $REPO_DIR
origin: https://github.com/weegeeday/PrusaKLoad.git
primary_branch: main
managed_services: $KLIPPER_SERVICE
install_script: install.sh
EOF
        ok "Added [update_manager prusa_kload] to $MOONRAKER_CONF"
        MOONRAKER_UPDATED=1
    fi
fi

restart_service "$KLIPPER_SERVICE"
if [[ "$MOONRAKER_UPDATED" -eq 1 ]]; then
    restart_service "$MOONRAKER_SERVICE"
fi

echo
ok "PrusaKLoad installed."
PRINTER_CFG="$CONFIG_PATH/printer.cfg"
if [[ -f "$PRINTER_CFG" ]] && grep -Eq '^\s*\[include\s+prusa_kload\.cfg\]' "$PRINTER_CFG"; then
    info "Found [include prusa_kload.cfg] in $PRINTER_CFG"
else
    info "Next steps:"
    echo "  1. Add to $PRINTER_CFG:"
    echo "       [include prusa_kload.cfg]"
    echo "       [include prusa_kload_menu.cfg]  # Optional, for physical Klipper LCDs"
    echo "  2. Edit $CONFIG_PATH/prusa_kload.cfg to set your filament_sensor (or switch_pin)."
    echo "  3. Restart Klipper (FIRMWARE_RESTART)."
fi
