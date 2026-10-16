#!/usr/bin/env bash
set -euo pipefail

# AntMonitor user-level installer
# Installs into $HOME/.antmonitor, creates venv, installs deps, creates CLI wrapper at ~/.local/bin/antmonitor
# Creates a systemd --user service to manage the app.

PROJ_DIR="$HOME/.antmonitor"
REPO_URL="https://github.com/raphael2025/antmonitor.git"
CLI_BIN="$HOME/.local/bin/antmonitor"
SERVICE_NAME="antmonitor.service"
SERVICE_PATH="$HOME/.config/systemd/user/$SERVICE_NAME"
VENV_PY="$PROJ_DIR/.venv/bin/python"

usage(){
  cat <<EOF
Usage: $0 [install|start|stop|status|restart|update|reset-password|uninstall|help]
Commands:
  install         Perform full user-level install (clone, venv, deps, service)
  start           Start the user service
  stop            Stop the user service
  status          Show service status and process info
  restart         Restart the service
  update          Pull latest code, reinstall deps if needed, restart
  reset-password  Generate a new admin password and store in credentials file
  uninstall       Stop service and remove installed files (asks confirmation)
  help            Show this help
EOF
}

# Ensure necessary commands are available or attempt to install via sudo
ensure_command(){
  local cmd="$1" pkg="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Command '$cmd' not found."
    if [ -n "${SUDO_ALLOWED:-1}" ] && sudo -n true 2>/dev/null; then
      echo "Installing package: $pkg"
      sudo apt-get update -y || true
      sudo apt-get install -y "$pkg"
    else
      echo "Please install '$pkg' (e.g. sudo apt-get install $pkg) and re-run."
      exit 1
    fi
  fi
}

# Create project dir and clone or pull
clone_or_update_repo(){
  if [ -d "$PROJ_DIR/.git" ]; then
    echo "Updating existing repository in $PROJ_DIR"
    git -C "$PROJ_DIR" pull --rebase --autostash || true
  else
    echo "Cloning repository into $PROJ_DIR"
    git clone "$REPO_URL" "$PROJ_DIR"
  fi
}

create_venv_and_install(){
  if [ ! -d "$PROJ_DIR/.venv" ]; then
    echo "Creating virtual environment"
    python3 -m venv "$PROJ_DIR/.venv"
  fi
  echo "Activating venv and installing Python dependencies"
  "$PROJ_DIR/.venv/bin/pip" install --upgrade pip
  if [ -f "$PROJ_DIR/requirements.txt" ]; then
    "$PROJ_DIR/.venv/bin/pip" install -r "$PROJ_DIR/requirements.txt"
  else
    echo "requirements.txt not found in repo; skipping pip install"
  fi
}

setup_config_and_language(){
  # Copy example config if no config exists
  if [ ! -f "$PROJ_DIR/config.yaml" ] && [ -f "$PROJ_DIR/config.example.yaml" ]; then
    cp "$PROJ_DIR/config.example.yaml" "$PROJ_DIR/config.yaml"
    echo "Created config.yaml from config.example.yaml"
  fi

  echo "Select UI language for AntMonitor (affects default docs and displayed language):"
  PS3="请选择语言 (输入数字): "
  options=("中文" "English" "Русский" "Español" "Deutsch" "العربية" "跳过/Keep existing")
  select opt in "${options[@]}"; do
    case "$REPLY" in
      1) LANG_CODE="zh"; break;;
      2) LANG_CODE="en"; break;;
      3) LANG_CODE="ru"; break;;
      4) LANG_CODE="es"; break;;
      5) LANG_CODE="de"; break;;
      6) LANG_CODE="ar"; break;;
      7) LANG_CODE=""; break;;
      *) echo "无效选择";;
    esac
  done

  if [ -n "$LANG_CODE" ]; then
    # store chosen language in .antmonitor/.lang for wrapper to read
    echo "$LANG_CODE" > "$PROJ_DIR/.lang"
    echo "Language set to $LANG_CODE"
  else
    echo "Keeping existing language settings"
  fi
}

create_user_service(){
  mkdir -p "$(dirname "$SERVICE_PATH")"
  cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=AntMonitor user service
After=network.target

[Service]
Type=simple
Restart=on-failure
RestartSec=5
WorkingDirectory=%h/.antmonitor
Environment=PYTHONUNBUFFERED=1
ExecStart=%h/.antmonitor/.venv/bin/python %h/.antmonitor/server.py

[Install]
WantedBy=default.target
EOF
  echo "Created user systemd unit: $SERVICE_PATH"
  # Reload systemd user units and enable
  systemctl --user daemon-reload || true
  systemctl --user enable --now "$SERVICE_NAME" || true
}

create_cli_wrapper(){
  mkdir -p "$(dirname "$CLI_BIN")"
  cat > "$CLI_BIN" <<'EOF'
#!/usr/bin/env bash
# AntMonitor CLI wrapper - forwards commands to the installer script or systemd --user
HOME_DIR="$HOME"
INSTALLER="$HOME_DIR/.antmonitor/install.sh"
SERVICE="antmonitor.service"

if [ "$#" -eq 0 ]; then
  set -- status
fi
case "$1" in
  install|update|reset-password|uninstall)
    shift
    "$INSTALLER" "$1" "$@"
    ;;
  start|stop|status|restart)
    case "$1" in
      status)
        systemctl --user status "$SERVICE" || true
        ps aux | grep -v grep | grep -E "[p]ython .*server.py" || true
        ;;
      *)
        systemctl --user "$1" "$SERVICE" || true
        ;;
    esac
    ;;
  help|-h|--help)
    "$INSTALLER" help
    ;;
  *)
    echo "Unknown command: $1"
    echo "Usage: antmonitor {install|start|stop|status|restart|update|reset-password|uninstall}"
    exit 2
    ;;
esac
EOF
  chmod +x "$CLI_BIN"
  echo "Created CLI wrapper at $CLI_BIN"
}

generate_admin_credentials(){
  CRED_FILE="$PROJ_DIR/admin_credentials.txt"
  if [ ! -f "$CRED_FILE" ]; then
    ADMIN_USER="admin"
    ADMIN_PASS=$(head -c 32 /dev/urandom | sha256sum | cut -c1-12)
    cat > "$CRED_FILE" <<EOT
# AntMonitor admin credentials (generated by install script)
username: $ADMIN_USER
password: $ADMIN_PASS
EOT
    chmod 600 "$CRED_FILE"
    echo "Generated admin credentials at $CRED_FILE"
  else
    echo "Admin credentials already exist at $CRED_FILE"
  fi
  echo "Admin username and password (stored in $CRED_FILE):"
  sed -n '1,3p' "$CRED_FILE" || true
}

cmd_install(){
  echo "Starting user-level install into $PROJ_DIR"
  # Ensure basic commands
  if [ -f /etc/debian_version ]; then
    SUDO_ALLOWED=1
  else
    SUDO_ALLOWED=0
  fi
  ensure_command git git
  ensure_command python3 python3
  ensure_command python3 -m venv python3-venv || true
  # python3 -m venv uses python3, so ensure python3-venv package
  if ! python3 -m venv --help >/dev/null 2>&1; then
    if [ "$SUDO_ALLOWED" -eq 1 ]; then
      sudo apt-get update -y || true
      sudo apt-get install -y python3-venv || true
    else
      echo "Python venv support not available; please install 'python3-venv' package and re-run."
      exit 1
    fi
  fi

  clone_or_update_repo
  create_venv_and_install
  setup_config_and_language
  generate_admin_credentials
  create_user_service
  create_cli_wrapper

  echo "Install complete. Add $HOME/.local/bin to your PATH if not present."
  echo "You can now run: antmonitor status"
}

cmd_start(){
  systemctl --user start "$SERVICE_NAME" || true
  echo "Started $SERVICE_NAME"
}

cmd_stop(){
  systemctl --user stop "$SERVICE_NAME" || true
  echo "Stopped $SERVICE_NAME"
}

cmd_status(){
  systemctl --user status "$SERVICE_NAME" || true
  ps aux | grep -v grep | grep -E "[p]ython .*server.py" || true
}

cmd_restart(){
  systemctl --user restart "$SERVICE_NAME" || true
  echo "Restarted $SERVICE_NAME"
}

cmd_update(){
  if [ -d "$PROJ_DIR" ]; then
    echo "Updating repository and reinstalling requirements"
    git -C "$PROJ_DIR" pull --rebase --autostash || true
    if [ -f "$PROJ_DIR/requirements.txt" ]; then
      "$PROJ_DIR/.venv/bin/pip" install -r "$PROJ_DIR/requirements.txt"
    fi
    systemctl --user restart "$SERVICE_NAME" || true
    echo "Update complete"
  else
    echo "Project not installed. Run '$0 install' first."
    exit 1
  fi
}

cmd_reset_password(){
  if [ -d "$PROJ_DIR" ]; then
    ADMIN_PASS=$(head -c 32 /dev/urandom | sha256sum | cut -c1-12)
    ADMIN_USER="admin"
    CRED_FILE="$PROJ_DIR/admin_credentials.txt"
    cat > "$CRED_FILE" <<EOT
# AntMonitor admin credentials (generated by install script)
username: $ADMIN_USER
password: $ADMIN_PASS
EOT
    chmod 600 "$CRED_FILE"
    echo "New admin credentials written to $CRED_FILE"
    echo "username: $ADMIN_USER"
    echo "password: $ADMIN_PASS"
    echo "Note: depending on application config, you may still need to set the password in the web UI on first login."
  else
    echo "Project not installed. Run '$0 install' first."
    exit 1
  fi
}

cmd_uninstall(){
  echo "UNINSTALL will stop the service and remove $PROJ_DIR and CLI."
  read -p "Are you sure? Type 'yes' to continue: " ans
  if [ "$ans" != "yes" ]; then
    echo "Aborted."
    exit 0
  fi
  systemctl --user stop "$SERVICE_NAME" || true
  systemctl --user disable "$SERVICE_NAME" || true
  rm -f "$SERVICE_PATH" || true
  systemctl --user daemon-reload || true
  rm -rf "$PROJ_DIR"
  rm -f "$CLI_BIN"
  echo "Uninstalled. Files removed: $PROJ_DIR, $CLI_BIN, $SERVICE_PATH"
}

case "${1:-help}" in
  install)
    cmd_install
    ;;
  start)
    cmd_start
    ;;
  stop)
    cmd_stop
    ;;
  status)
    cmd_status
    ;;
  restart)
    cmd_restart
    ;;
  update)
    cmd_update
    ;;
  reset-password)
    cmd_reset_password
    ;;
  uninstall)
    cmd_uninstall
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
