#!/bin/bash
# sync-local.sh — 把本仓 packages/dsh-plugin 同步进本机已安装的 DSH profile。
#
# 为什么需要：profile 的依赖是 `file:`，pnpm 安装时是**复制**进
# node_modules，不是软链。改完源码不跑本脚本，DSH 设置页读到的仍是旧
# bundle（2026-10-22 实测：缓存开关与付费额度都已改，面板仍显示旧版）。
#
# 用法：
#   packages/dsh-plugin/scripts/sync-local.sh            # 同步 web+desktop（存在才同步）
#   packages/dsh-plugin/scripts/sync-local.sh web        # 只同步指定 profile
#   packages/dsh-plugin/scripts/sync-local.sh --check    # 只比对，不写；有漂移退出 1
#   packages/dsh-plugin/scripts/sync-local.sh --restart  # 同步后重启 DSH web
#
# 环境变量：DSH_HOME 覆盖 ~/.dsh；DSH_PROFILES 逗号分隔（默认 web,desktop）。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
MODE="sync"
RESTART=0
PROFILES=()

for arg in "$@"; do
  case "$arg" in
    --check) MODE="check" ;;
    --restart) RESTART=1 ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) PROFILES+=("$arg") ;;
  esac
done

if [ ${#PROFILES[@]} -eq 0 ]; then
  IFS=',' read -r -a PROFILES <<<"${DSH_PROFILES:-web,desktop}"
fi

TS="$(date +%Y%m%d-%H%M%S)"
drift=0

for p in "${PROFILES[@]}"; do
  DEST="$DSH_HOME/profiles/$p/node_modules/@taxueseek/argo-dsh"
  if [ ! -d "$DEST" ]; then
    echo "[skip] $p: 未安装 $DEST"
    continue
  fi
  if [ "$MODE" = check ]; then
    if diff -rq "$SRC/dsh" "$DEST/dsh" >/dev/null 2>&1 \
       && cmp -s "$SRC/package.json" "$DEST/package.json"; then
      echo "[ok] $p"
    else
      echo "[drift] $p: 源码与已安装 bundle 不一致，跑一次不带 --check 的同步"
      drift=1
    fi
    continue
  fi
  cp -a "$DEST" "$DSH_HOME/backups/argo-dsh-sync-$TS-$p"
  rsync -a --delete "$SRC/dsh/" "$DEST/dsh/"
  rsync -a "$SRC/package.json" "$SRC/cordis.patch.yml" "$SRC/README.md" "$DEST/"
  echo "[synced] $p"
done

if [ "$MODE" = check ]; then
  exit "$drift"
fi

if [ "$RESTART" = 1 ] && [ -x "$DSH_HOME/scripts/argo-dsh-restart.sh" ]; then
  echo "[restart] $DSH_HOME/scripts/argo-dsh-restart.sh"
  bash "$DSH_HOME/scripts/argo-dsh-restart.sh"
else
  echo "重启 DSH web 后生效：bash $DSH_HOME/scripts/argo-dsh-restart.sh"
fi
