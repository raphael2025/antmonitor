#!/usr/bin/env bash
# AntMonitor · Ubuntu 一键安装 / 管理脚本
#
# 一行安装(不用先 git clone，没装 git 也行)：
#   curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash
# 首次交互安装会询问云端接入码，粘贴后自动连接；直接回车可跳过。
# 已安装后只连接/更换云端，不更新程序：
#   curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/install.sh | sudo bash -s -- connect
#
# 再跑一次：已安装时出管理菜单(更新 / 修复 / 卸载 / 重置账号 / 连接云端)；没有终端时直接更新。
# 也可直接指定：... | sudo bash -s -- install|update|repair|uninstall|reset-user|connect|menu
# 选项：--lang zh|en|ru|es|de|ar  --dir 安装目录  --branch 分支
#       --connect-code 接入码  --site-name 场地名  --site-type air|hydro|mixed
#       --source auto|github|gitee（默认 auto；已安装保持 origin）
# 未指定源码平台时，从已下载/克隆的目录运行 sudo bash install.sh 会用本地源码(pip 依赖仍需联网或镜像)。
# 环境变量：GH_PROXY=https://镜像前缀/  访问 GitHub 很慢时用(镜像能看到并篡改下载内容，只用信得过的)
#           ANTMONITOR_SOURCE=github|gitee 与 --source 等效；ANTMONITOR_REPO 可指定自定义 Git 仓库。
# Gitee 安装会自动安装 Git；Git 不可用或克隆失败时停止，不改从其它平台下载。
set -euo pipefail

APP_NAME="AntMonitor"
INSTALL_DIR="${ANTMONITOR_HOME:-/opt/antmonitor}"
SERVICE_NAME="antmonitor"
GH_REPO="${ANTMONITOR_GH_REPO:-raphael2025/antmonitor}"
GH_PROXY="${GH_PROXY:-}"
SOURCE="${ANTMONITOR_SOURCE:-auto}"
SOURCE_EXPLICIT=0
[[ -n "${ANTMONITOR_SOURCE:-}" ]] && SOURCE_EXPLICIT=1
# 生产默认跟踪 main(dev 是开发分支，未经验证)。已安装的现场更新时沿用当前分支，见 sync_code
BRANCH="${ANTMONITOR_BRANCH:-main}"
PY_MIN="3.8"

ACTION=""
LANG_CODE=""
CONNECT_CODE=""
SITE_NAME=""
SITE_TYPE=""
CLOUD_SETUP_CHANGED=0
GENERATED_PW=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    install|update|repair|uninstall|reset-user|connect|configure-cloud|menu) ACTION="$1" ;;
    --lang) LANG_CODE="${2:-}"; shift ;;
    --dir) INSTALL_DIR="${2:-}"; shift ;;
    --branch) BRANCH="${2:-}"; ANTMONITOR_BRANCH="$BRANCH"; shift ;;
    --source)
      [[ $# -ge 2 && -n "${2:-}" ]] || { echo "--source 需要一个值：auto、github 或 gitee" >&2; exit 2; }
      SOURCE="$2"; SOURCE_EXPLICIT=1; shift ;;
    --connect-code)
      [[ $# -ge 2 && -n "${2:-}" ]] || { echo "--connect-code 需要接入码，或用 - 从终端粘贴" >&2; exit 2; }
      CONNECT_CODE="$2"; shift ;;
    --site-name) SITE_NAME="${2:-}"; shift ;;
    --site-type)
      [[ $# -ge 2 && "${2:-}" =~ ^(air|hydro|mixed)$ ]] || { echo "--site-type 只能是 air、hydro 或 mixed" >&2; exit 2; }
      SITE_TYPE="$2"; shift ;;
    -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null || true; exit 0 ;;
    *) echo "未知参数 / unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
# Resolve relative destinations before any function changes its working directory.
[[ -n "$INSTALL_DIR" ]] || { echo '--dir 不能为空' >&2; exit 2; }
[[ "$INSTALL_DIR" == /* ]] || INSTALL_DIR="$PWD/$INSTALL_DIR"

# 源码平台与下载方式必须一致：Gitee 公开归档链接可能返回下载网页，因此使用
# Git 获取源码；不能把返回的 HTML 当源码或在失败后悄悄回到 GitHub。
ARCHIVE_URL=""
case "$SOURCE" in
  auto|github)
    SOURCE_REPO_URL="${GH_PROXY}https://github.com/${GH_REPO}.git"
    ARCHIVE_URL="${GH_PROXY}https://github.com/${GH_REPO}/archive/refs/heads/${BRANCH}.tar.gz"
    ;;
  gitee) SOURCE_REPO_URL="https://gitee.com/raphael01/antmonitor.git" ;;
  *) echo "--source / ANTMONITOR_SOURCE 只能是 auto、github 或 gitee" >&2; exit 2 ;;
esac
if [[ "$SOURCE_EXPLICIT" == 1 && "$SOURCE" == github && "$GH_REPO" != raphael2025/antmonitor ]]; then
  echo "显式选择官方源码平台时不能同时指定自定义 ANTMONITOR_GH_REPO；请只保留一种配置" >&2
  exit 2
fi
REPO_URL="${ANTMONITOR_REPO:-$SOURCE_REPO_URL}"
if [[ "$SOURCE_EXPLICIT" == 1 && "$REPO_URL" != "$SOURCE_REPO_URL" ]]; then
  echo "显式选择源码平台时不能同时指定不同的 ANTMONITOR_REPO；自定义仓库请不要设置 --source / ANTMONITOR_SOURCE" >&2
  exit 2
fi
# 自定义仓库继续用于 Git 克隆，但不能在失败后下载另一个官方仓库的源码。
[[ "$REPO_URL" != "$SOURCE_REPO_URL" ]] && ARCHIVE_URL=""
export GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never
export GIT_HTTP_LOW_SPEED_LIMIT="${GIT_HTTP_LOW_SPEED_LIMIT:-1000}" GIT_HTTP_LOW_SPEED_TIME="${GIT_HTTP_LOW_SPEED_TIME:-30}"
AUTO_SOURCE=0
if [[ "$SOURCE" == auto && "$REPO_URL" == "$SOURCE_REPO_URL" && "$GH_REPO" == raphael2025/antmonitor ]]; then
  AUTO_SOURCE=1
fi

# 未显式选择平台时，从已克隆/解压的仓库根目录运行仍优先用本地源码。
# 显式 --source / ANTMONITOR_SOURCE 必须从所选平台获取代码，不能被本地副本覆盖。
# curl | bash 时没有脚本文件(BASH_SOURCE 为空)，绝不能把当前目录误当源码
LOCAL_SRC=""
if [[ "$SOURCE_EXPLICIT" == 0 && -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
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

    zh:m6) echo "连接 / 更换云端" ;;
    en:m6) echo "Connect / change cloud" ;;
    ru:m6) echo "Подключить / сменить облако" ;;
    es:m6) echo "Conectar / cambiar nube" ;;
    de:m6) echo "Cloud verbinden / wechseln" ;;
    ar:m6) echo "اتصال / تغيير السحابة" ;;

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
  # Gitee / 自定义仓库必须用 Git；已有 Git 部署也不能因 Git 缺失而被源码包覆盖。
  if ! apt-get install -y -qq git >/dev/null 2>&1; then
    if command -v git >/dev/null; then
      warn "Git 软件包安装/更新失败，继续使用已安装的 Git"
    elif [[ -e "$INSTALL_DIR/.git" || -z "$ARCHIVE_URL" ]]; then
      die "当前源码来源需要 Git，自动安装失败；请检查 apt 软件源、安装 git 后重试"
    else
      warn "git 装不上，改用 GitHub 源码包(网页更新不可用，可再跑本命令更新)"
    fi
  fi
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

official_origin() {
  local url="${1%/}"
  url="${url%.git}"
  case "$url" in
    https://github.com/raphael2025/antmonitor|http://github.com/raphael2025/antmonitor|\
    git@github.com:raphael2025/antmonitor|ssh://git@github.com/raphael2025/antmonitor|\
    https://gitee.com/raphael01/antmonitor|http://gitee.com/raphael01/antmonitor|\
    git@gitee.com:raphael01/antmonitor|ssh://git@gitee.com/raphael01/antmonitor) return 0 ;;
  esac
  [[ -n "$GH_PROXY" && "$url" == "${GH_PROXY}https://github.com/raphael2025/antmonitor" ]]
}

sync_origin_source() {
  [[ -e "$INSTALL_DIR/.git" ]] || return 0
  command -v git >/dev/null || die "安装目录已有 Git 仓库，但找不到 git 命令；请先安装 git，现有源码保持不变"
  [[ "$SOURCE_EXPLICIT" == 1 && "$SOURCE" != auto ]] || return 0
  local origin
  local g=(git -c safe.directory="$INSTALL_DIR" -C "$INSTALL_DIR")
  origin="$("${g[@]}" remote get-url origin)" || die "无法读取现有 origin，已停止自动换源"
  official_origin "$origin" || die "现有 origin 是自定义仓库，已保留；请移除 --source / ANTMONITOR_SOURCE，或人工确认后修改 origin"
  if [[ "$origin" != "$REPO_URL" ]]; then
    "${g[@]}" remote set-url origin "$REPO_URL"
    info "更新源已切换为 $SOURCE"
  fi
}

sync_code() {
  mkdir -p "$INSTALL_DIR"
  sync_origin_source
  if [[ -n "$LOCAL_SRC" && "$(cd "$LOCAL_SRC" && pwd -P)" == "$(cd "$INSTALL_DIR" && pwd -P)" ]]; then
    info "使用安装目录里现有的本地源码"
    return
  fi
  if [[ -n "$LOCAL_SRC" && ! -d "$LOCAL_SRC/.git" && -e "$INSTALL_DIR/.git" ]]; then
    die "目标已是 Git 仓库，不能用不含版本信息的本地源码覆盖；请在独立目录安装或使用 Git 快进更新"
  fi
  if [[ -n "$LOCAL_SRC" && -d "$LOCAL_SRC/.git" && -e "$INSTALL_DIR/.git" \
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
    sync_origin_source
  elif [[ -e "$INSTALL_DIR/.git" ]]; then
    # 沿用现场当前分支(网页更新也是跟它)；只有显式设了 ANTMONITOR_BRANCH 才切换分支
    local br
    local g=(git -c safe.directory="$INSTALL_DIR" -C "$INSTALL_DIR")
    br="$("${g[@]}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
    if [[ -n "${ANTMONITOR_BRANCH:-}" || "$br" == "HEAD" ]]; then
      br="$BRANCH"
    fi
    if ! "${g[@]}" fetch --prune origin; then
      local origin alternate current
      origin="$("${g[@]}" remote get-url origin)"
      [[ "$AUTO_SOURCE" == 1 ]] && official_origin "$origin" \
        || die "从现有 origin 拉取失败：请检查网络与仓库访问权限，代码未更新"
      "${g[@]}" diff --quiet && "${g[@]}" diff --cached --quiet \
        || die "工作区有本地改动，自动备用源更新已停止"
      current="$("${g[@]}" rev-parse --abbrev-ref HEAD)"
      [[ "$current" == "$br" && "$current" != HEAD ]] || die "备用源更新不能自动切换分支，请检查原来源"
      if [[ "$origin" == *gitee.com* ]]; then alternate="${GH_PROXY}https://github.com/raphael2025/antmonitor.git"
      else alternate="https://gitee.com/raphael01/antmonitor.git"; fi
      info "原来源不可达，尝试备用来源（保留 origin）：$alternate"
      "${g[@]}" fetch "$alternate" "$br" || die "GitHub/Gitee 均无法拉取，现有代码和配置保留"
      "${g[@]}" merge --ff-only FETCH_HEAD || die "备用来源无法快进，现有版本保留，不会回退或覆盖改动"
      return
    fi
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
      die "从现有 origin 更新失败：请检查网络、权限或本地分支，服务尚未重启"
  elif [[ -f "$INSTALL_DIR/server.py" && -n "$ARCHIVE_URL" ]]; then
    # GitHub 源码包安装继续用源码包；显式 Gitee / 自定义 Git 源走下方克隆路径，
    # 让旧的无 .git 安装也能切换来源并获得后续 Git 更新能力。
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
      if [[ "$AUTO_SOURCE" == 1 ]]; then
        tmpc="$(mktemp -d)"
        info "GitHub 不可达，尝试 Gitee"
        if git clone -q --branch "$BRANCH" https://gitee.com/raphael01/antmonitor.git "$tmpc/src"; then
          cp -a "$tmpc/src"/. "$INSTALL_DIR"/
          rm -rf "$tmpc"
          return
        fi
        rm -rf "$tmpc"
      fi
      [[ -n "$ARCHIVE_URL" ]] || die "Git 克隆失败，当前源码来源没有可用的源码包兜底；请检查网络与仓库访问权限后重试"
      warn "git clone 失败，改用 GitHub 源码包下载"
    fi
    fetch_tarball
  fi
}

fetch_tarball() {
  [[ -n "$ARCHIVE_URL" ]] || die "当前源码来源需要 Git；请安装 git 并确认仓库可访问后重试"
  local tmp url
  tmp="$(mktemp -d)"
  url="$ARCHIVE_URL"
  info "下载源码包 $url"
  if ! curl -fsSL --retry 3 --connect-timeout 15 "$url" | tar xz -C "$tmp" --strip-components=1; then
    rm -rf "$tmp"
    die "下载失败：检查本机能否访问 GitHub；国内网络可设 GH_PROXY 镜像前缀后重试，或下载 ZIP 解压后在目录里运行 sudo bash install.sh"
  fi
  [[ -f "$tmp/server.py" && -f "$tmp/requirements.txt" ]] || { rm -rf "$tmp"; die "下载的源码包不完整，现有文件未修改"; }
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
  local wheels="${ANTMONITOR_WHEELHOUSE:-}" opts=()
  [[ -n "$wheels" || ! -d "$INSTALL_DIR/offline/wheels" ]] || wheels="$INSTALL_DIR/offline/wheels"
  if [[ -n "$wheels" ]]; then
    [[ -d "$wheels" ]] || die "依赖缓存目录不存在：$wheels"
    opts=(--no-index --find-links "$wheels")
  else
    "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
  fi
  "$INSTALL_DIR/.venv/bin/pip" install -q "${opts[@]}" -r "$INSTALL_DIR/requirements.txt"
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

apply_connect_code() {  # optional=仅处理显式接入码；prompt=主动询问；后两参为现有服务用户/组
  CLOUD_SETUP_CHANGED=0
  local mode="${1:-optional}" run_user="${2:-}" run_group="${3:-}" tty=0
  local code="${CONNECT_CODE:-${ANTMONITOR_CONNECT_CODE:-}}"
  has_tty && tty=1
  if [[ "$code" == "-" && "$tty" == 0 ]]; then
    warn "--connect-code - 需要终端，请在 SSH 终端运行后粘贴接入码；未修改云端配置"
    return 1
  fi
  [[ -n "$code" || "$mode" == prompt ]] || return 0
  if [[ -z "$code" && "$tty" == 0 ]]; then
    # 自动化安装不增加交互；独立 connect 会在调用前提示需要接入码。
    return 0
  fi
  local py out rc=0; py="$(py_bin)"
  # 逻辑随一键脚本分发：旧安装也能直接 connect，不需要先下载新模块或更新程序。
  # 接入码通过环境传给子进程，不进入 Python 命令行；正常交互从 /dev/tty 隐藏读取。
  out="$(cd "$INSTALL_DIR" && AM_CONNECT_CODE="$code" AM_SITE_NAME="$SITE_NAME" \
    AM_SITE_TYPE="$SITE_TYPE" AM_HAS_TTY="$tty" AM_RUN_USER="$run_user" AM_RUN_GROUP="$run_group" \
    "$py" - <<'PY_CLOUD_SETUP' 2>&1
import base64
import binascii
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
from urllib.parse import urlsplit

SITE_TYPES = ("air", "hydro", "mixed")


class SetupError(ValueError):
    pass


def site_key(value):
    """与云端 validate.site_key 一致，仅用于校验，不改写身份；允许中文及中间空格。"""
    if value is None:
        return ""
    text = "".join(ch for ch in str(value) if ch >= " " and ch != "\x7f").strip()[:65]
    return text if 0 < len(text) <= 64 else ""


class Terminal:
    """在降为服务账户前打开终端；sudo 管道安装也不会从脚本 stdin 取输入。"""
    def __init__(self):
        self.reader = open("/dev/tty", "r", encoding="utf-8")
        try:
            self.writer = open("/dev/tty", "w", encoding="utf-8")
        except BaseException:
            self.reader.close()
            raise

    def write(self, text):
        self.writer.write(text + "\n")
        self.writer.flush()

    def ask(self, text, secret=False):
        import termios
        previous = None
        if secret:
            previous = termios.tcgetattr(self.reader.fileno())
            hidden = list(previous)
            hidden[3] &= ~termios.ECHO
            termios.tcsetattr(self.reader.fileno(), termios.TCSANOW, hidden)
        try:
            self.writer.write(text)
            self.writer.flush()
            value = self.reader.readline()
        finally:
            if previous is not None:
                termios.tcsetattr(self.reader.fileno(), termios.TCSADRAIN, previous)
                self.write("")
        if not value:
            raise EOFError
        return value.strip()

    def close(self):
        self.reader.close()
        self.writer.close()


def use_service_account(user, group):
    """整个配置过程按现有服务身份运行，原子替换的 0600 文件也归该账户。"""
    if not user:
        return
    import grp
    import pwd
    account = pwd.getpwuid(int(user)) if user.isdigit() else pwd.getpwnam(user)
    gid = (int(group) if group.isdigit() else grp.getgrnam(group).gr_gid) if group else account.pw_gid
    if os.geteuid() == account.pw_uid and os.getegid() == gid:
        return
    if os.geteuid() == 0:
        os.initgroups(account.pw_name, gid)
        os.setgid(gid)
        os.setuid(account.pw_uid)
    elif os.geteuid() != account.pw_uid or os.getegid() != gid:
        raise SetupError("无法切换到现有服务账户，请使用 sudo 运行")


def decode_code(code):
    code = code.strip()
    if len(code) > 16384 or not re.fullmatch(r"AMC1-[A-Za-z0-9_-]+", code):
        raise SetupError("接入码格式不对，应以 AMC1- 开头")
    try:
        encoded = code[5:]
        data = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4),
                                          altchars=b"-_", validate=True))
    except (ValueError, UnicodeError, binascii.Error):
        raise SetupError("接入码解不开，请从云端完整复制") from None
    if not isinstance(data, dict):
        raise SetupError("接入码内容不完整")
    if not isinstance(data.get("u"), str) or not isinstance(data.get("t"), str):
        raise SetupError("接入码缺少云端地址或上报凭据")
    url, token = data["u"].strip().rstrip("/"), data["t"].strip()
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise SetupError("接入码中的云端地址格式不对")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in token):
        raise SetupError("接入码中的上报凭据格式不对")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        parsed.port  # 校验端口，避免保存无法使用的地址
    except ValueError:
        raise SetupError("接入码中的云端地址格式不对") from None
    if (parsed.scheme not in ("http", "https") or not host or parsed.username
            or parsed.password or parsed.query or parsed.fragment or not token):
        raise SetupError("接入码中的云端地址或上报凭据不完整")
    if parsed.scheme == "http":
        try:
            private = ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
        except ValueError:
            private = host == "localhost"
        if not private:
            raise SetupError("公网云端必须使用 HTTPS，请从云端重新复制接入码")
    if any(data.get(key) is not None and not isinstance(data[key], str) for key in ("f", "s", "n", "y")):
        raise SetupError("接入码中的场地信息格式不对")
    fp = cloud_report.normalize_fingerprint(data.get("f"))
    if fp is None:
        raise SetupError("接入码中的证书指纹格式不对")
    if data.get("s") is not None and data["s"] != "":
        sid = data["s"]
        if not sid or site_key(sid) != sid:
            raise SetupError("接入码中的场地 ID 格式不对")
        data["s"] = sid
    name = data.get("n") or ""
    if len(name.strip()) > 64 or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise SetupError("接入码中的场地名称格式不对")
    if data.get("y") is not None and data["y"] not in SITE_TYPES:
        raise SetupError("接入码中的场地类型须为 air、hydro 或 mixed")
    data.update(u=url, t=token, f=fp)
    return data


def read_state():
    # load_settings() 会容忍损坏文件并回退为空；绑定必须严格读，避免丢失原配置。
    try:
        with open(appconfig.SETTINGS_FILE, encoding="utf-8") as source:
            saved = json.load(source)
    except FileNotFoundError:
        saved = {}
    if not isinstance(saved, dict) or ("cloud" in saved and not isinstance(saved["cloud"], dict)):
        raise SetupError("现有 settings.json 格式异常，已保留原文件")
    try:
        cfg = appconfig.load_config()
        appconfig.apply_settings(cfg, saved)
    except (appconfig.yaml.YAMLError, TypeError, AttributeError):
        raise SetupError("现有配置文件格式异常，已保留原文件") from None
    return dict(cfg.get("cloud") or {})


def candidate(data, current, name="", site_type="", terminal=None):
    # 显式参数 > 新码携带的名称/类型 > 现有有效配置 > 新安装默认值。
    chosen_name = name.strip() or str(data.get("n") or "").strip()
    default_name = str(current.get("site_name") or "").strip() or socket.gethostname().split(".")[0] or "site"
    if not chosen_name:
        chosen_name = (terminal.ask("场地名称 [" + default_name + "]，回车保留: ")
                       if terminal else "") or default_name
    chosen_type = site_type or data.get("y")
    if not chosen_type:
        default_type = current.get("site_type") if current.get("site_type") in SITE_TYPES else "air"
        chosen_type = default_type
        if terminal:
            while True:
                answer = terminal.ask("场地类型 1=风冷 2=水冷 3=混合 [" +
                                      str(SITE_TYPES.index(default_type) + 1) + "]，回车保留: ")
                chosen_type = {"1": "air", "2": "hydro", "3": "mixed"}.get(answer, answer) or default_type
                if chosen_type in SITE_TYPES:
                    break
                terminal.write("请输入 1、2、3，或直接回车保留。")
    if chosen_type not in SITE_TYPES:
        raise SetupError("场地类型须为 air、hydro 或 mixed")
    cloud = dict(current)
    cloud.update(enabled=True, url=data["u"], token=data["t"], tls_fingerprint=data["f"],
                 site_name=chosen_name[:64], site_type=chosen_type)
    sid = data.get("s") or current.get("site_id")
    if not sid:
        try:
            with open(cloud_report._ID_FILE, encoding="ascii") as source:
                sid = source.read().strip()
        except FileNotFoundError:
            pass
        except UnicodeError:
            raise SetupError("现有场地 ID 文件内容异常，已保留原配置") from None
    if sid:
        sid = str(sid)
        if site_key(sid) != sid:
            raise SetupError("现有场地 ID 格式异常，请从云端复制本场地的专属接入码")
    # 候选身份只在测试通过后随 settings 一起保存；失败不生成/改写 cloud_site_id.txt。
    cloud["site_id"] = sid or "st-" + secrets.token_hex(6)
    return cloud


def configure(code, name="", site_type="", terminal=None):
    code = code.strip()
    if code == "-":
        if terminal is None:
            raise SetupError("--connect-code - 需要终端，请在 SSH 终端运行后粘贴接入码")
        code = ""
    while True:
        if not code:
            if terminal is None:
                raise SetupError("请在 SSH 终端运行 connect 并粘贴接入码")
            code = terminal.ask("粘贴云端接入码（AMC1-…，输入不显示，回车跳过）: ", secret=True)
            if not code:
                print("已跳过连接云端，原配置未修改。")
                return 10
        data = {}
        try:
            data = decode_code(code)
            current = read_state()
            cloud = candidate(data, current, name, site_type, terminal)
            if terminal:
                terminal.write("正在验证云端连接…")
            passed, message = cloud_report.test(cloud, cloud["site_id"])
            if not passed:
                raise SetupError("连接验证未通过：" + message)
            # 测试期间若另一位管理员修改了配置，先重新读取，而不是覆盖新设置。
            latest = read_state()
            if latest != current:
                raise SetupError("云端配置刚被其他操作修改，请重新粘贴接入码后重试")
            appconfig.update_settings({"cloud": cloud})
            print("连接验证通过，云端配置已保存（场地：" + cloud["site_name"] + "）。")
            return 0
        except (SetupError, OSError, ValueError, RuntimeError) as error:
            message = str(error) + "；未修改原云端配置。"
            for secret in (code, data.get("t")):
                if secret:
                    message = message.replace(secret, "[已隐藏]")
            if terminal is None:
                print(message)
                return 1
            terminal.write(message + "\n请重新粘贴接入码，或直接回车跳过。")
            code = ""


def main():
    terminal = None
    try:
        if os.environ.get("AM_HAS_TTY") == "1":
            terminal = Terminal()
        use_service_account(os.environ.get("AM_RUN_USER", ""), os.environ.get("AM_RUN_GROUP", ""))
        sys.path.insert(0, ".")
        global appconfig, cloud_report
        import appconfig
        import cloud_report
        return configure(os.environ.get("AM_CONNECT_CODE", ""), os.environ.get("AM_SITE_NAME", ""),
                         os.environ.get("AM_SITE_TYPE", ""), terminal)
    except (EOFError, KeyboardInterrupt):
        print("已取消连接云端，原配置未修改。")
        return 10
    except Exception as error:
        print("无法配置云端：" + str(error) + "；未修改原云端配置。")
        return 1
    finally:
        if terminal:
            terminal.close()


if __name__ == "__main__":
    sys.exit(main())
PY_CLOUD_SETUP
  )" || rc=$?
  case "$rc" in
    0) CLOUD_SETUP_CHANGED=1; ok "$out" ;;
    10) info "$out" ;;
    *) warn "$out"; return 1 ;;
  esac
}

do_connect() {
  info "$(msg m6)"
  [[ -f "$INSTALL_DIR/server.py" && -f "$INSTALL_DIR/config.yaml" ]] || die "未找到已安装的 AntMonitor：$INSTALL_DIR"
  if ! has_tty && [[ -z "${CONNECT_CODE:-${ANTMONITOR_CONNECT_CODE:-}}" ]]; then
    warn "连接云端需要粘贴接入码，请在 SSH 终端运行 connect；未修改云端配置"
    return 1
  fi
  local state svc_user svc_group
  state="$(systemctl show -p LoadState --value "${SERVICE_NAME}.service" 2>/dev/null)" || {
    warn "无法读取现有服务，请确认 AntMonitor 已安装为 systemd 服务"; return 1;
  }
  [[ "$state" == loaded ]] || { warn "未找到已安装的 systemd 服务，请先完成安装"; return 1; }
  svc_user="$(systemctl show -p User --value "${SERVICE_NAME}.service")" || return 1
  svc_group="$(systemctl show -p Group --value "${SERVICE_NAME}.service")" || return 1
  # systemd 的 User 为空表示 root；Group 为空时 Python 使用该账户的主组。
  apply_connect_code prompt "${svc_user:-root}" "$svc_group" || return 1
  [[ "$CLOUD_SETUP_CHANGED" == 1 ]] || return 0
  if ! systemctl restart "${SERVICE_NAME}.service"; then
    warn "云端配置已保存，但服务重启失败；请查看 journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
    return 1
  fi
  sleep 1
  if ! systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    warn "云端配置已保存，但服务尚未运行；请查看 journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
    return 1
  fi
  ok "服务已启动；本地完成扫描后自动上报，可在云端查看场地状态。"
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
  local cloud_mode=optional cloud_rc=0
  [[ -f "$INSTALL_DIR/server.py" ]] || cloud_mode=prompt
  ensure_packages
  sync_code
  [[ -f "$INSTALL_DIR/server.py" ]] || die "代码下载不完整：$INSTALL_DIR"
  setup_venv
  ensure_config
  if admin_unusable; then set_admin_password; fi
  apply_connect_code "$cloud_mode" || cloud_rc=$?
  write_systemd
  start_svc
  if [[ -n "$GENERATED_PW" ]]; then
    echo "  ${BLD}登录账号:${RST} admin  /  ${BLD}${GENERATED_PW}${RST}  (自动生成，请马上记下，登录后可在面板里改)"
  fi
  ok "安装完成。打开面板 →「⚙ 网段」填矿机网段。"
  if [[ "$CLOUD_SETUP_CHANGED" == 1 ]]; then
    info "云端配置已保存；本地完成扫描后自动上报，可在云端查看场地状态。"
  else
    info "稍后可再次运行本命令，选择「连接 / 更换云端」并粘贴接入码。"
  fi
  return "$cloud_rc"
}

do_update() {
  echo
  info "$(msg m2)"
  [[ -d "$INSTALL_DIR" ]] || die "未安装: $INSTALL_DIR"
  # 先在服务运行中拉代码、装依赖，全部成功后再重启：以前先 stop，断网等任一步失败
  # (set -e 立即退出) 服务就一直停着，监控停摆没人知道
  sync_code
  setup_venv
  local cloud_rc=0
  apply_connect_code || cloud_rc=$?
  write_systemd
  start_svc
  ok "更新完成"
  return "$cloud_rc"
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
  echo "  6) $(msg m6)"
  echo "  0) $(msg m0)"
  echo
  local choice=""
  ask choice "$(msg menu) [0-6] " || exit 0
  case "${choice:-}" in
    1) do_install ;;
    2) do_update ;;
    3) do_repair ;;
    4) do_uninstall ;;
    5) do_reset_user ;;
    6) do_connect || true ;;
    0) exit 0 ;;
    *) warn "无效选项" ;;
  esac
}

main() {
  need_root
  if [[ "${CONNECT_CODE:-${ANTMONITOR_CONNECT_CODE:-}}" == "-" ]] && ! has_tty; then
    die "--connect-code - 需要终端，请在 SSH 终端运行后粘贴接入码；未修改云端配置"
  fi
  if [[ -n "$LANG_CODE" ]]; then :
  elif [[ -z "$ACTION" ]] && has_tty && [[ -f "$INSTALL_DIR/server.py" ]]; then pick_language
  else LANG_CODE="zh"; fi
  if [[ -z "$ACTION" ]]; then
    # 没装过：直接装(一行命令装好，不先进菜单)；装过了：有终端出菜单，没终端就更新
    if [[ ! -f "$INSTALL_DIR/server.py" ]]; then ACTION="install"
    elif [[ -n "${CONNECT_CODE:-${ANTMONITOR_CONNECT_CODE:-}}" ]]; then ACTION="connect"
    elif has_tty; then ACTION="menu"
    else ACTION="update"; fi
  fi
  case "$ACTION" in
    install) do_install; return ;;
    update) do_update; return ;;
    repair) do_repair; return ;;
    uninstall) do_uninstall; return ;;
    reset-user) do_reset_user; return ;;
    connect|configure-cloud) do_connect; return ;;
  esac
  while true; do
    show_menu
    echo
    local pause=""
    ask pause "按回车返回菜单…" || exit 0
  done
}

main "$@"
