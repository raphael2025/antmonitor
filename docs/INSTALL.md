# AntMonitor — user-level one-line installer

This branch adds a user-level one-line installer that installs AntMonitor into the current user's home directory and creates an `antmonitor` CLI wrapper.

Quick install (run as your normal user):

bash <(curl -sL https://raw.githubusercontent.com/raphael2025/antmonitor/feature/user-installer/install.sh) install

Commands provided by the CLI wrapper (~/.local/bin/antmonitor):
- install
- start
- stop
- status
- restart
- update
- reset-password
- uninstall

Notes:
- The installer will place files under ~/.antmonitor and create a systemd --user service. Make sure `~/.local/bin` is in your PATH.
- The script will attempt to use sudo to install system packages (git, python3-venv) when available on Debian/Ubuntu systems. If sudo is not available, follow the script prompts to install missing tools.
- Admin credentials are generated and stored at ~/.antmonitor/admin_credentials.txt. Depending on application behavior you may still need to set the password via the web UI on first run — the file is a convenience to record the generated credentials.

