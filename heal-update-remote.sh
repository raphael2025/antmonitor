#!/usr/bin/env bash
# AntMonitor · 把官方 SSH origin 改成匿名 HTTPS，让「旧网页更新器」也能拉到新版本。
#
# 适用：面板「⬆ 版本」报 Permission denied (publickey)，但机器能访问 GitHub 或 Gitee。
# 本脚本只改 git remote / insteadOf，不改业务配置、不重启服务。改完后到面板再点检查/更新即可
# 升到新更新器；之后会自动走 HTTPS，不再需要 SSH key。
#
#   curl -fsSL https://raw.githubusercontent.com/raphael2025/antmonitor/main/heal-update-remote.sh | sudo bash
#   curl -fsSL https://gitee.com/raphael01/antmonitor/raw/main/heal-update-remote.sh | sudo bash
#   sudo bash heal-update-remote.sh --dir /opt/antmonitor
set -euo pipefail

INSTALL_DIR="${ANTMONITOR_HOME:-/opt/antmonitor}"
# 兼容历史目录名
if [[ ! -e "$INSTALL_DIR/.git" && -e /opt/miner-monitor/app/.git ]]; then
  INSTALL_DIR=/opt/miner-monitor/app
fi
GITHUB_HTTPS="https://github.com/raphael2025/antmonitor.git"
GITEE_HTTPS="https://gitee.com/raphael01/antmonitor.git"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="${2:-}"; shift ;;
    -h|--help)
      sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
  shift
done

[[ "$INSTALL_DIR" == /* ]] || INSTALL_DIR="$PWD/$INSTALL_DIR"
[[ -e "$INSTALL_DIR/.git" ]] || { echo "不是 git 安装目录: $INSTALL_DIR" >&2; exit 1; }
command -v git >/dev/null || { echo "找不到 git" >&2; exit 1; }

g=(git -c safe.directory="$INSTALL_DIR" -C "$INSTALL_DIR")
origin="$("${g[@]}" remote get-url origin)"
norm=""
case "${origin%/}" in
  *.git) base="${origin%.git}" ;;
  *) base="${origin%/}" ;;
esac
case "$base" in
  https://github.com/raphael2025/antmonitor|http://github.com/raphael2025/antmonitor|\
  git@github.com:raphael2025/antmonitor|ssh://git@github.com/raphael2025/antmonitor|\
  git@github.com-*:raphael2025/antmonitor|ssh://git@github.com-*/raphael2025/antmonitor)
    norm="$GITHUB_HTTPS" ;;
  https://gitee.com/raphael01/antmonitor|http://gitee.com/raphael01/antmonitor|\
  git@gitee.com:raphael01/antmonitor|ssh://git@gitee.com/raphael01/antmonitor|\
  git@gitee.com-*:raphael01/antmonitor|ssh://git@gitee.com-*/raphael01/antmonitor)
    norm="$GITEE_HTTPS" ;;
esac

if [[ -z "$norm" ]]; then
  echo "origin 不是官方仓库，已保留: $origin"
  exit 0
fi

if [[ "$origin" != "$norm" ]]; then
  "${g[@]}" remote set-url origin "$norm"
  echo "origin: $origin"
  echo "    →  $norm"
else
  echo "origin 已是匿名 HTTPS: $norm"
fi

# 旧 SSH URL 的 insteadOf，避免其它脚本仍拼 SSH 地址
if [[ "$norm" == "$GITHUB_HTTPS" ]]; then
  "${g[@]}" config "url.$GITHUB_HTTPS.insteadOf" "git@github.com:raphael2025/antmonitor.git"
  "${g[@]}" config --add "url.$GITHUB_HTTPS.insteadOf" "git@github.com-miner-monitor:raphael2025/antmonitor.git"
else
  "${g[@]}" config "url.$GITEE_HTTPS.insteadOf" "git@gitee.com:raphael01/antmonitor.git"
fi

export GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never
if "${g[@]}" fetch --quiet --prune origin; then
  echo "fetch origin 成功。请打开面板「⬆ 程序版本更新」检查/更新，即可切到新更新器。"
else
  alt="$GITEE_HTTPS"
  [[ "$norm" == "$GITEE_HTTPS" ]] && alt="$GITHUB_HTTPS"
  echo "原 HTTPS 源不可达，试备用来源: $alt"
  "${g[@]}" fetch --quiet "$alt" "+refs/heads/*:refs/remotes/origin/*"
  "${g[@]}" remote set-url origin "$alt"
  echo "已切换 origin → $alt。请打开面板检查/更新。"
fi
