#!/usr/bin/env bash
# AntMonitor · Ubuntu 一键管理脚本
# 用法: sudo bash install.sh
# 菜单: 选语言 → 首次安装 / 更新 / 修复 / 卸载 / 重置用户名与密码
set -euo pipefail

APP_NAME="AntMonitor"
INSTALL_DIR="${ANTMONITOR_HOME:-/opt/antmonitor}"
SERVICE_NAME="antmonitor"
REPO_URL="${ANTMONITOR_REPO:-https://github.com/raphael2025/antmonitor.git}"
BRANCH="${ANTMONITOR_BRANCH:-dev}"
PY_MIN="3.8"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 若从已克隆的仓库根目录运行，优先用本地源码安装/更新
LOCAL_SRC=""
if [[ -f "$SCRIPT_DIR/server.py" && -f "$SCRIPT_DIR/requirements.txt" ]]; then
  LOCAL_SRC="$SCRIPT_DIR"
fi

LANG_CODE="zh"
RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; CYN=$'\033[36m'; BLD=$'\033[1m'; RST=$'\033[0m'

msg() { # key → 按 LANG_CODE 取文案
  local k="$1"
  case "$LANG_CODE:$k" in
    zh:title) echo "AntMonitor · Ubuntu 一键管理" ;;
    en:title) echo "AntMonitor · Ubuntu setup" ;;
    ru:title) echo "AntMonitor · установка Ubuntu" ;;
    es:title) echo "AntMonitor · instalación Ubuntu" ;;
    de:title) echo "AntMonitor · Ubuntu-Setup" ;;
    ar:title) echo "AntMonitor · إعداد Ubuntu" ;;

    zh:pick_lang) echo "请选择语言 / Choose language:" ;;
    en:pick_lang) echo "Choose language:" ;;
    ru:pick_lang) echo "Выберите язык:" ;;
    es:pick_lang) echo "Elija idioma:" ;;
    de:pick_lang) echo "Sprache wählen:" ;;
    ar:pick_lang) echo "اختر اللغة:" ;;

    zh:menu) echo "请选择操作:" ;;
    en:menu) echo "Select an action:" ;;
    ru:menu) echo "Выберите действие:" ;;
    es:menu) echo "Seleccione una acción:" ;;
    de:menu) echo "Aktion wählen:" ;;
    ar:menu) echo "اختر عملية:" ;;

    zh:m1) echo "首次安装" ;;
    en:m1) echo "Install (first time)" ;;
    ru:m1) echo "Первая установка" ;;
    es:m1) echo "Instalación inicial" ;;
    de:m1) echo "Ersteinrichtung" ;;
    ar:m1) echo "تثبيت أول مرة" ;;

    zh:m2) echo "更新" ;;
    en:m2) echo "Update" ;;
    ru:m2) echo "Обновить" ;;
    es:m2) echo "Actualizar" ;;
    de:m2) echo "Aktualisieren" ;;
    ar:m2) echo "تحديث" ;;

    zh:m3) echo "修复" ;;
    en:m3) echo "Repair" ;;
    ru:m3) echo "Восстановить" ;;
    es:m3) echo "Reparar" ;;
    de:m3) echo "Reparieren" ;;
    ar:m3) echo "إصلاح" ;;

    zh:m4) echo "卸载" ;;
    en:m4) echo "Uninstall" ;;
    ru:m4) echo "Удалить" ;;
    es:m4) echo "Desinstalar" ;;
    de:m4) echo "Deinstallieren" ;;
    ar:m4) echo "إزالة" ;;

    zh:m5) echo "重置用户名和密码" ;;
    en:m5) echo "Reset username & password" ;;
    ru:m5) echo "Сброс логина и пароля" ;;
    es:m5) echo "Restablecer usuario y contraseña" ;;
    de:m5) echo "Benutzer & Passwort zurücksetzen" ;;
    ar:m5) echo "إعادة تعيين المستخدم وكلمة المرور" ;;

    zh:m0) echo "退出" ;;
    en:m0) echo "Exit" ;;
    ru:m0) echo "Выход" ;;
    es:m0) echo "Salir" ;;
    de:m0) echo "Beenden" ;;
    ar:m0) echo "خروج" ;;

    zh:need_root) echo "请使用 root 运行: sudo bash install.sh" ;;
    en:need_root) echo "Run as root: sudo bash install.sh" ;;
    *) echo "$k" ;;
  esac
}

info()  { echo "${CYN}→${RST} $*"; }
ok()    { echo "${GRN}✓${RST} $*"; }
warn()  { echo "${YLW}!${RST} $*"; }
die()   { echo "${RED}✗${RST} $*" >&2; exit 1; }

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    die "$(msg need_root)"
  fi
}

pick_language() {
  echo
  echo "${BLD}$(msg pick_lang)${RST}"
  echo "  1) 中文     2) English  3) Русский"
  echo "  4) Español  5) Deutsch  6) العربية"
  read -r -p "> " c || true
  case "${c:-1}" in
    1) LANG_CODE=zh ;;
    2) LANG_CODE=en ;;
    3) LANG_CODE=ru ;;
    4) LANG_CODE=es ;;
    5) LANG_CODE=de ;;
    6) LANG_CODE=ar ;;
    *) LANG_CODE=zh ;;
  esac
}

ensure_packages() {
  info "apt: python3 / venv / git / curl …"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates >/dev/null
  ok "依赖已就绪"
}

py_bin() {
  if [[ -x "$INSTALL_DIR/.venv/bin/python" ]]; then
    echo "$INSTALL_DIR/.venv/bin/python"
  else
    echo "python3"
  fi
}

write_systemd() {
  local user="${SUDO_USER:-root}"
  # 目录属主：尽量用调用 sudo 的人，否则 root
  if id "$user" &>/dev/null && [[ "$user" != "root" ]]; then
    :
  else
    user="root"
  fi
  chown -R "$user:$user" "$INSTALL_DIR" || true
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=${APP_NAME}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${user}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/.venv/bin/python server.py
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}.service" >/dev/null
  ok "systemd: ${SERVICE_NAME}.service (User=${user})"
}

sync_code() {
  mkdir -p "$INSTALL_DIR"
  if [[ -n "$LOCAL_SRC" ]]; then
    info "从本地仓库同步 → $INSTALL_DIR"
    rsync -a --delete \
      --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
      --exclude 'miner_monitor.db*' --exclude 'logs/' \
      --exclude 'settings.json' --exclude 'segments.json' \
      --exclude 'config.yaml' --exclude 'config.yaml.bak' \
      --exclude 'cloud_site_id.txt' \
      "$LOCAL_SRC"/ "$INSTALL_DIR"/
    # 保留 .git 便于日后 update（若目标尚无）
    if [[ -d "$LOCAL_SRC/.git" && ! -d "$INSTALL_DIR/.git" ]]; then
      rsync -a "$LOCAL_SRC/.git"/ "$INSTALL_DIR/.git"/
    fi
  elif [[ -d "$INSTALL_DIR/.git" ]]; then
    info "git pull ($BRANCH)"
    git -C "$INSTALL_DIR" fetch --all --prune
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH" || \
      warn "pull 失败（私有仓需部署密钥/Token）。可在有代码的机器上再跑本脚本。"
  else
    info "git clone $REPO_URL"
    if ! git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"; then
      die "克隆失败。私有仓库请先配置 git 凭证，或在已 clone 的目录里执行: sudo bash install.sh"
    fi
  fi
}

setup_venv() {
  info "创建/更新虚拟环境与依赖"
  if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
    python3 -m venv "$INSTALL_DIR/.venv"
  fi
  "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
  "$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
  ok "Python 依赖 OK"
}

ensure_config() {
  if [[ ! -f "$INSTALL_DIR/config.yaml" ]]; then
    cp "$INSTALL_DIR/config.example.yaml" "$INSTALL_DIR/config.yaml"
    ok "已生成 config.yaml（请按场地改网段；可用菜单重置账号密码）"
  else
    info "保留已有 config.yaml"
  fi
}

start_svc() {
  systemctl restart "${SERVICE_NAME}.service"
  sleep 1
  if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    ok "服务已启动"
    local ip
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo
    echo "  ${BLD}面板 Panel:${RST}  http://127.0.0.1:8800"
    [[ -n "${ip:-}" ]] && echo "               http://${ip}:8800"
    echo
  else
    warn "服务未处于 active，请查看: journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
  fi
}

do_install() {
  echo
  info "$(msg m1) → $INSTALL_DIR"
  ensure_packages
  sync_code
  setup_venv
  ensure_config
  write_systemd
  start_svc
  ok "安装完成。建议立刻用菜单「重置用户名和密码」改掉默认口令。"
}

do_update() {
  echo
  info "$(msg m2)"
  [[ -d "$INSTALL_DIR" ]] || die "未安装: $INSTALL_DIR"
  systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || true
  sync_code
  setup_venv
  write_systemd
  start_svc
  ok "更新完成"
}

do_repair() {
  echo
  info "$(msg m3)"
  [[ -d "$INSTALL_DIR" ]] || die "未安装: $INSTALL_DIR"
  ensure_packages
  systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || true
  rm -rf "$INSTALL_DIR/.venv"
  setup_venv
  ensure_config
  write_systemd
  start_svc
  ok "修复完成（已重建 venv）"
}

do_uninstall() {
  echo
  warn "$(msg m4)"
  read -r -p "确认卸载服务？数据目录可选择保留 [y/N] " a || true
  [[ "${a:-}" =~ ^[Yy]$ ]] || { info "已取消"; return; }
  systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || true
  systemctl disable "${SERVICE_NAME}.service" 2>/dev/null || true
  rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
  systemctl daemon-reload
  read -r -p "同时删除 $INSTALL_DIR （含数据库）？不可恢复 [y/N] " b || true
  if [[ "${b:-}" =~ ^[Yy]$ ]]; then
    rm -rf "$INSTALL_DIR"
    ok "已删除 $INSTALL_DIR"
  else
    ok "已移除服务；目录保留: $INSTALL_DIR"
  fi
}

do_reset_user() {
  echo
  info "$(msg m5)"
  [[ -f "$INSTALL_DIR/config.yaml" ]] || die "找不到 config.yaml"
  local py; py="$(py_bin)"
  echo "当前用户:"
  "$py" - <<'PY' "$INSTALL_DIR"
import sys, os
os.chdir(sys.argv[1])
sys.path.insert(0, ".")
import appconfig
cfg = appconfig.load_config()
for u in (cfg.get("auth") or {}).get("users") or []:
    print(f"  - {u.get('username')}  ({u.get('role')})")
PY
  echo
  read -r -p "要修改的用户名（现有）: " old_u
  [[ -n "${old_u:-}" ]] || die "未输入用户名"
  read -r -p "新用户名（直接回车=不改名）: " new_u || true
  new_u="${new_u:-$old_u}"
  local pass1 pass2
  read -r -s -p "新密码: " pass1; echo
  read -r -s -p "再输一次: " pass2; echo
  [[ "$pass1" == "$pass2" ]] || die "两次密码不一致"
  [[ -n "$pass1" ]] || die "密码不能为空"

  OLD_U="$old_u" NEW_U="$new_u" NEW_PW="$pass1" "$py" - <<'PY' "$INSTALL_DIR"
import os, sys
os.chdir(sys.argv[1])
sys.path.insert(0, ".")
import appconfig, auth

old_u = os.environ["OLD_U"]
new_u = os.environ["NEW_U"]
pw = os.environ["NEW_PW"]

err = auth.password_problem(new_u, pw)
if err:
    print(err)
    sys.exit(1)

cfg = appconfig.load_config()
users = (cfg.get("auth") or {}).get("users") or []
found = None
for u in users:
    if u.get("username") == old_u:
        found = u
        break
if not found:
    print(f"没有用户 {old_u}")
    sys.exit(1)

if new_u != old_u:
    if any(u.get("username") == new_u for u in users):
        print(f"用户名已存在: {new_u}")
        sys.exit(1)
    # 改名：写回 yaml（保留其它字段）
    import re, shutil
    path = appconfig.CONFIG_FILE
    text = open(path, encoding="utf-8").read()
    # 只替换对应用户块里的 username 行（简单场景）
    pat = re.compile(
        r'(username:\s*)(["\']?)' + re.escape(old_u) + r'\2',
        re.M)
    text2, n = pat.subn(r'\1\g<2>' + new_u + r'\2', text, count=1)
    if n != 1:
        print("改名失败：未能在 config.yaml 唯一定位 username，请手改")
        sys.exit(1)
    shutil.copy2(path, path + ".bak")
    open(path, "w", encoding="utf-8").write(text2)
    print(f"用户名: {old_u} → {new_u}")

err = appconfig.set_user_password(new_u, auth.hash_password(pw))
if err:
    print("写密码失败:", err)
    sys.exit(1)
print("密码已更新（备份 config.yaml.bak）。正在重启服务…")
PY
  systemctl restart "${SERVICE_NAME}.service" 2>/dev/null || true
  ok "账号已更新，请用新用户名/密码登录面板"
}

show_menu() {
  clear 2>/dev/null || true
  echo "${BLD}========================================${RST}"
  echo "${BLD}  $(msg title)${RST}"
  echo "${BLD}========================================${RST}"
  echo "  安装目录: $INSTALL_DIR"
  echo "  服务名:   ${SERVICE_NAME}.service"
  echo
  echo "  1) $(msg m1)"
  echo "  2) $(msg m2)"
  echo "  3) $(msg m3)"
  echo "  4) $(msg m4)"
  echo "  5) $(msg m5)"
  echo "  0) $(msg m0)"
  echo
  read -r -p "$(msg menu) [0-5] " choice || true
  case "${choice:-}" in
    1) do_install ;;
    2) do_update ;;
    3) do_repair ;;
    4) do_uninstall ;;
    5) do_reset_user ;;
    0) exit 0 ;;
    *) warn "无效选项" ;;
  esac
}

main() {
  need_root
  pick_language
  while true; do
    show_menu
    echo
    read -r -p "按回车返回菜单…" _ || true
  done
}

main "$@"
