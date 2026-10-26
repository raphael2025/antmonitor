#!/usr/bin/env bash
# AntMonitor · Ubuntu 一键安装 / 管理脚本
#
# 一行安装(不用先 git clone，没装 git 也行)：
#   curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash
# 安装时顺便接入云端(接入码在云端面板「🔌 场地接入」里复制；"-" = 在终端里粘贴，不留在命令历史里)：
#   curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash -s -- --connect-code - --site-name 内蒙一场
#
# 再跑一次：已安装时出管理菜单(更新 / 修复 / 卸载 / 重置账号)；没有终端时直接更新。
# 也可直接指定：... | sudo bash -s -- install|update|repair|uninstall|reset-user|menu
# 选项：--lang zh|en|ru|es|de|ar  --dir 安装目录  --branch 分支
#       --connect-code 接入码  --site-name 场地名  --site-type air|hydro|mixed
# 从已下载/克隆的源码目录运行 sudo bash install.sh 时，直接用这份源码(不用访问 GitHub；pip 依赖仍需联网或镜像)。
# 环境变量：GH_PROXY=https://镜像前缀/  访问 GitHub 很慢时用(镜像能看到并篡改下载内容，只用信得过的)
set -euo pipefail

APP_NAME="AntMonitor"
INSTALL_DIR="${ANTMONITOR_HOME:-/opt/antmonitor}"
SERVICE_NAME="antmonitor"
GH_REPO="${ANTMONITOR_GH_REPO:-raphael2025/antmonitor}"
GH_PROXY="${GH_PROXY:-}"
REPO_URL="${ANTMONITOR_REPO:-${GH_PROXY}https://github.com/${GH_REPO}.git}"
# 生产默认跟踪 main(dev 是开发分支，未经验证)。已安装的现场更新时沿用当前分支，见 sync_code
BRANCH="${ANTMONITOR_BRANCH:-main}"
PY_MIN="3.8"

ACTION=""
LANG_CODE=""
CONNECT_CODE=""
SITE_NAME=""
SITE_TYPE="air"
GENERATED_PW=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    install|update|repair|uninstall|reset-user|menu) ACTION="$1" ;;
    --lang) LANG_CODE="${2:-}"; shift ;;
    --dir) INSTALL_DIR="${2:-}"; shift ;;
    --branch) BRANCH="${2:-}"; ANTMONITOR_BRANCH="$BRANCH"; shift ;;
    --connect-code) CONNECT_CODE="${2:-}"; shift ;;
    --site-name) SITE_NAME="${2:-}"; shift ;;
    --site-type) SITE_TYPE="${2:-air}"; shift ;;
    -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null || true; exit 0 ;;
    *) echo "未知参数 / unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# 若从已克隆/解压的仓库根目录运行，优先用本地源码安装/更新。
# curl | bash 时没有脚本文件(BASH_SOURCE 为空)，绝不能把当前目录误当源码
LOCAL_SRC=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -f "$SCRIPT_DIR/server.py" && -f "$SCRIPT_DIR/requirements.txt" ]]; then
    LOCAL_SRC="$SCRIPT_DIR"
  fi
fi

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

# curl | bash 时标准输入是脚本本身：交互一律从 /dev/tty 读；没有终端(自动化部署)时返回失败用默认值
has_tty() { ( exec </dev/tty ) 2>/dev/null; }
ask() {  # ask 变量名 提示 [silent]
  local __v=""
  has_tty || return 1
  if [[ "${3:-}" == "silent" ]]; then read -r -s -p "$2" __v </dev/tty; echo >/dev/tty
  else read -r -p "$2" __v </dev/tty; fi
  printf -v "$1" '%s' "$__v"
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
  local c=""
  ask c "> " || true
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
  apt-get update -qq || warn "apt-get update 失败，继续尝试"
  apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates tar >/dev/null
  # git 只用于网页一键更新；装不上(内网源缺包等)就改用源码包，安装照样完成
  apt-get install -y -qq git >/dev/null 2>&1 || warn "git 装不上，改用源码包下载(网页更新不可用，可再跑本命令更新)"
  apt-get install -y -qq rsync >/dev/null 2>&1 || true
  # 依赖(fastapi 等)最低要 Python 3.8：Ubuntu 18.04 自带 3.6，不拦住的话会在 pip 安装时
  # 报一堆看不懂的版本错误
  if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= tuple(map(int, '${PY_MIN}'.split('.'))) else 1)"; then
    die "系统 python3 版本 $(python3 -V 2>&1) 低于 ${PY_MIN}：请升级到 Ubuntu 20.04 及以上，或安装 python3.8+ 后重试"
  fi
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
  if [[ -n "$LOCAL_SRC" && -d "$LOCAL_SRC/.git" && -d "$INSTALL_DIR/.git" \
        && "$(cd "$LOCAL_SRC" && pwd -P)" != "$(cd "$INSTALL_DIR" && pwd -P)" ]]; then
    # 两边都是 git 仓库：按提交快进，不用 rsync 覆盖工作区。rsync 覆盖后工作区与安装目录
    # 自己的 HEAD 对不上，git status 永远是脏的，网页更新会一直以"工作区有改动"拒绝；
    # 若网页更新已让安装目录领先，rsync 还会悄悄把代码降级
    info "从本地仓库快进合并 → $INSTALL_DIR"
    # 以 root 操作普通用户的仓库，新版 git 会报 dubious ownership；命令行显式信任这两个目录
    local g=(git -c safe.directory="$INSTALL_DIR" -c safe.directory="$LOCAL_SRC" -C "$INSTALL_DIR")
    if "${g[@]}" fetch "$LOCAL_SRC" HEAD; then
      local src_br
      src_br="$(git -c safe.directory="$LOCAL_SRC" -C "$LOCAL_SRC" rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
      if "${g[@]}" merge --ff-only FETCH_HEAD; then
        :
      elif [[ "$src_br" != "HEAD" && -z "$("${g[@]}" rev-list --no-merges FETCH_HEAD..HEAD)" ]]; then
        # 快进不了但安装目录没有本地源码里没有的实际改动(典型：旧版装在 dev，只多出合并提交)：
        # 直接切到本地源码所在的分支和提交，否则会"提示更新完成、实际还是旧代码"
        info "安装目录切换到 $src_br 分支（与本地源码一致）"
        "${g[@]}" checkout -B "$src_br" FETCH_HEAD
        "${g[@]}" rev-parse --verify --quiet "origin/$src_br" >/dev/null \
          && "${g[@]}" branch --set-upstream-to="origin/$src_br" "$src_br" >/dev/null || true
      else
        warn "无法快进(安装目录有本地源码里没有的改动)，代码保持不变"
      fi
    else
      warn "从 $LOCAL_SRC 拉取失败，代码保持不变"
    fi
  elif [[ -n "$LOCAL_SRC" ]]; then
    info "从本地仓库同步 → $INSTALL_DIR"
    if ! command -v rsync >/dev/null; then
      # 最小化系统可能没有 rsync：用 tar 覆盖复制(不删除安装目录里多出的旧文件)
      tar -C "$LOCAL_SRC" --exclude=./.venv --exclude=./__pycache__ --exclude=./.git \
        --exclude='./miner_monitor.db*' --exclude=./logs --exclude=./settings.json \
        --exclude=./segments.json --exclude=./config.yaml --exclude=./config.yaml.bak \
        --exclude=./cloud_site_id.txt --exclude=./backups --exclude=./certs \
        --exclude=./.update_state.json -cf - . | tar -C "$INSTALL_DIR" -xf -
      return
    fi
    rsync -a --delete \
      --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
      --exclude 'miner_monitor.db*' --exclude 'logs/' \
      --exclude 'settings.json' --exclude 'segments.json' \
      --exclude 'config.yaml' --exclude 'config.yaml.bak' \
      --exclude 'cloud_site_id.txt' --exclude 'backups/' --exclude 'certs/' \
      --exclude '.update_state.json' \
      "$LOCAL_SRC"/ "$INSTALL_DIR"/
    # 保留 .git 便于日后 update（若目标尚无）
    if [[ -d "$LOCAL_SRC/.git" && ! -d "$INSTALL_DIR/.git" ]]; then
      rsync -a "$LOCAL_SRC/.git"/ "$INSTALL_DIR/.git"/
    fi
  elif [[ -d "$INSTALL_DIR/.git" ]]; then
    # 沿用现场当前分支(网页更新也是跟它)；只有显式设了 ANTMONITOR_BRANCH 才切换分支
    local br
    local g=(git -c safe.directory="$INSTALL_DIR" -C "$INSTALL_DIR")
    br="$("${g[@]}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
    if [[ -n "${ANTMONITOR_BRANCH:-}" || "$br" == "HEAD" ]]; then
      br="$BRANCH"
    fi
    "${g[@]}" fetch --all --prune || warn "fetch 失败（私有仓需部署密钥/Token / 网络不通）"
    # 旧版本脚本默认装在 dev，dev 已不再更新：dev 上没有 main 里没有的实际改动时改跟 main
    # (与网页更新器的规则一致，见 updater._can_leave_legacy)
    if [[ -z "${ANTMONITOR_BRANCH:-}" && "$br" == "dev" ]] \
        && "${g[@]}" rev-parse --verify --quiet origin/main >/dev/null \
        && [[ -z "$("${g[@]}" rev-list --no-merges origin/main..HEAD)" ]]; then
      info "旧版安装在 dev 分支，改为跟踪 main"
      br="main"
    fi
    info "git pull ($br)"
    "${g[@]}" checkout "$br"
    "${g[@]}" pull --ff-only origin "$br" || \
      warn "pull 失败（私有仓需部署密钥/Token）。可在有代码的机器上再跑本脚本。"
  elif [[ -f "$INSTALL_DIR/server.py" ]]; then
    # 以前用源码包装的(没有 .git)：继续用源码包覆盖更新
    fetch_tarball
  else
    if command -v git >/dev/null; then
      # 先克隆到临时目录再搬过去：目标目录可能已存在且有别的东西(--dir /data)，
      # 直接 clone 进去必然失败，失败后更不能 rm -rf 它
      local tmpc
      tmpc="$(mktemp -d)"
      info "git clone $REPO_URL"
      if git clone -q --branch "$BRANCH" "$REPO_URL" "$tmpc/src"; then
        mkdir -p "$INSTALL_DIR"
        cp -a "$tmpc/src"/. "$INSTALL_DIR"/
        rm -rf "$tmpc"
        return
      fi
      rm -rf "$tmpc"
      warn "git clone 失败，改用源码包下载"
    fi
    fetch_tarball
  fi
}

fetch_tarball() {
  local tmp url
  tmp="$(mktemp -d)"
  url="${GH_PROXY}https://github.com/${GH_REPO}/archive/refs/heads/${BRANCH}.tar.gz"
  info "下载源码包 $url"
  if ! curl -fsSL --retry 3 --connect-timeout 15 "$url" | tar xz -C "$tmp" --strip-components=1; then
    rm -rf "$tmp"
    die "下载失败：检查本机能否访问 GitHub；国内网络可设 GH_PROXY 镜像前缀后重试，或下载 ZIP 解压后在目录里运行 sudo bash install.sh"
  fi
  mkdir -p "$INSTALL_DIR"
  # config.yaml / 数据库 / settings.json 等本机文件不在源码包里，不会被覆盖
  cp -a "$tmp"/. "$INSTALL_DIR"/
  rm -rf "$tmp"
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
  chmod 600 "$INSTALL_DIR/config.yaml"   # 含口令哈希/矿机密码
}

admin_unusable() {   # admin 口令还是示例占位/空/弱口令 → 需要设置
  local py; py="$(py_bin)"
  (cd "$INSTALL_DIR" && "$py" - <<'PY'
import sys
sys.path.insert(0, ".")
import appconfig, auth
cfg = appconfig.load_config()
u = next((u for u in cfg["auth"]["users"] if u.get("username") == "admin"), None)
if u is None:   # admin 已改名/删掉：不归这里管(用菜单「重置用户名和密码」)
    sys.exit(1)
sys.exit(0 if auth._unusable(str(u.get("password") or ""))
         or "admin" in auth.weak_default_users(cfg) else 1)
PY
  )
}

set_admin_password() {
  local p1="" p2="" py; py="$(py_bin)"
  while has_tty; do
    ask p1 "设置面板 admin 密码(直接回车=自动生成强密码): " silent || break
    [[ -z "$p1" ]] && break
    ask p2 "再输一次: " silent || break
    [[ "$p1" == "$p2" ]] && break
    warn "两次不一致"; p1=""
  done
  if [[ -z "$p1" ]]; then
    p1="$("$py" -c 'import secrets; print(secrets.token_urlsafe(12))')"
    GENERATED_PW="$p1"
  fi
  local out
  if ! out="$(cd "$INSTALL_DIR" && NEW_PW="$p1" "$py" - <<'PY' 2>&1
import os, sys
sys.path.insert(0, ".")
import appconfig, auth
pw = os.environ["NEW_PW"]
err = auth.password_problem("admin", pw)
if err:
    print(err); sys.exit(1)
cfg = appconfig.load_config()
if not any(u.get("username") == "admin" for u in cfg["auth"]["users"]):
    print("config.yaml 里没有 admin 用户"); sys.exit(1)
err = appconfig.set_user_password("admin", auth.hash_password(pw))
if err:
    print(err); sys.exit(1)
PY
  )"; then
    GENERATED_PW=""
    die "设置 admin 密码失败：$out"
  fi
  ok "admin 密码已设置"
}

apply_connect_code() {   # 把云端接入码写进 settings.json(与网页「☁ 云端上报」保存的是同一处)
  # 接入码含 token：写在命令行里本机其他用户用 ps 能看到、也会留在 shell 历史里。
  # --connect-code - 表示从终端输入；也可用环境变量 ANTMONITOR_CONNECT_CODE 传
  [[ -z "$CONNECT_CODE" && -n "${ANTMONITOR_CONNECT_CODE:-}" ]] && CONNECT_CODE="$ANTMONITOR_CONNECT_CODE"
  if [[ "$CONNECT_CODE" == "-" ]]; then
    CONNECT_CODE=""
    ask CONNECT_CODE "粘贴云端接入码(AMC1-…，输入不显示): " silent || true
  fi
  [[ -n "$CONNECT_CODE" ]] || return 0
  local py; py="$(py_bin)"
  local name="${SITE_NAME:-$(hostname -s 2>/dev/null || echo site)}"
  local out
  if out="$(cd "$INSTALL_DIR" && CODE="$CONNECT_CODE" NAME="$name" TYPE="$SITE_TYPE" "$py" - <<'PY' 2>&1
import base64, json, os, sys
sys.path.insert(0, ".")
import appconfig, cloud_report
code = os.environ["CODE"].strip()
if not code.startswith("AMC1-"):
    print("接入码格式不对(应以 AMC1- 开头)"); sys.exit(1)
b = code[5:]
try:
    d = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
except ValueError:
    print("接入码解不开，请从云端面板完整复制"); sys.exit(1)
fp = cloud_report.normalize_fingerprint(d.get("f"))
if not d.get("u") or not d.get("t") or fp is None:
    print("接入码内容不完整"); sys.exit(1)
cl = {"enabled": True, "url": str(d["u"]).rstrip("/"), "token": d["t"], "tls_fingerprint": fp,
      "site_name": os.environ["NAME"][:64],
      "site_type": os.environ["TYPE"] if os.environ["TYPE"] in ("air", "hydro", "mixed") else "air"}
if d.get("s"):
    cl["site_id"] = str(d["s"])
appconfig.update_settings({"cloud": cl})
ok, msg = cloud_report.test(cl, cl.get("site_id") or cloud_report.site_id({"cloud": cl}))
print(("✓ " if ok else "! ") + msg)
PY
  )"; then
    ok "已接入云端：$(echo "$out" | tail -1)"
  else
    warn "接入码没用上：$out(装好后可在面板「⚙ 网段 → ☁ 云端上报」里粘贴)"
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
  [[ -f "$INSTALL_DIR/server.py" ]] || die "代码下载不完整：$INSTALL_DIR"
  setup_venv
  ensure_config
  if admin_unusable; then set_admin_password; fi
  apply_connect_code
  write_systemd
  start_svc
  if [[ -n "$GENERATED_PW" ]]; then
    echo "  ${BLD}登录账号:${RST} admin  /  ${BLD}${GENERATED_PW}${RST}  (自动生成，请马上记下，登录后可在面板里改)"
  fi
  ok "安装完成。打开面板 →「⚙ 网段」填矿机网段；接云端在同一页「☁ 云端上报」粘贴接入码。"
}

do_update() {
  echo
  info "$(msg m2)"
  [[ -d "$INSTALL_DIR" ]] || die "未安装: $INSTALL_DIR"
  # 先在服务运行中拉代码、装依赖，全部成功后再重启：以前先 stop，断网等任一步失败
  # (set -e 立即退出) 服务就一直停着，监控停摆没人知道
  sync_code
  setup_venv
  apply_connect_code
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
  local a="" b=""
  ask a "确认卸载服务？数据目录可选择保留 [y/N] " || true
  [[ "${a:-}" =~ ^[Yy]$ ]] || { info "已取消"; return; }
  systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || true
  systemctl disable "${SERVICE_NAME}.service" 2>/dev/null || true
  rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
  systemctl daemon-reload
  ask b "同时删除 $INSTALL_DIR （含数据库）？不可恢复 [y/N] " || true
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
  has_tty || die "重置账号需要在终端里交互运行：sudo bash install.sh reset-user"
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
  local old_u="" new_u="" pass1="" pass2=""
  ask old_u "要修改的用户名（现有）: " || true
  [[ -n "${old_u:-}" ]] || die "未输入用户名"
  ask new_u "新用户名（直接回车=不改名）: " || true
  new_u="${new_u:-$old_u}"
  ask pass1 "新密码: " silent || true
  ask pass2 "再输一次: " silent || true
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
  # 上面以 root 原子替换了 config.yaml(属主会变成 root)：还给服务账号，否则服务读不了
  # 600 权限的配置起不来，或网页改密码时写不回去
  local svc_user
  svc_user="$(systemctl show -p User --value "${SERVICE_NAME}.service" 2>/dev/null || true)"
  svc_user="${svc_user:-root}"
  chown "$svc_user:$svc_user" "$INSTALL_DIR/config.yaml" "$INSTALL_DIR/config.yaml.bak" 2>/dev/null || true
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
  local choice=""
  ask choice "$(msg menu) [0-5] " || exit 0
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
  if [[ -n "$LANG_CODE" ]]; then :
  elif [[ -z "$ACTION" ]] && has_tty && [[ -f "$INSTALL_DIR/server.py" ]]; then pick_language
  else LANG_CODE="zh"; fi
  if [[ -z "$ACTION" ]]; then
    # 没装过：直接装(一行命令装好，不先进菜单)；装过了：有终端出菜单，没终端就更新
    if [[ ! -f "$INSTALL_DIR/server.py" ]]; then ACTION="install"
    elif has_tty; then ACTION="menu"
    else ACTION="update"; fi
  fi
  case "$ACTION" in
    install) do_install; return ;;
    update) do_update; return ;;
    repair) do_repair; return ;;
    uninstall) do_uninstall; return ;;
    reset-user) do_reset_user; return ;;
  esac
  while true; do
    show_menu
    echo
    local pause=""
    ask pause "按回车返回菜单…" || exit 0
  done
}

main "$@"
