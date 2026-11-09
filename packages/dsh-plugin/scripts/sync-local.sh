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
#
# 边界（2026-11-09）：本脚本只做产物同步，结果是 ARTIFACT_SYNCED，不是
# LIVE_ACTIVATION。不要在这里串重启、也不要手改 profile 的 node_modules 之外
# 的 Host 状态。本机 dsh CLI 只有 dsh plugin --profile <name> add|remove，
# 没有 restart 子命令；对同路径的 file: 依赖重复 add 会打印
# "Already up to date" 而不重拷，这正是本脚本存在的原因。
# 同步后核对同 PID 的 Host 行与页面功能；本脚本不证明插件已激活。
# 官方同 PID 默认见 devkit knowledge/contracts/live-activation.md 与
# pitfalls/file-copy-stale.md。若依赖改成 link: 安装，本脚本可整体删除。
#
# 环境变量：DSH_HOME 覆盖 ~/.dsh；DSH_PROFILES 逗号分隔（默认 web,desktop）。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
MODE="sync"
PROFILES=()

for arg in "$@"; do
  case "$arg" in
    --check) MODE="check" ;;
    --restart) echo "[removed] --restart 已移除：dsh CLI 无 restart 子命令，同步不等于激活，见脚本头部" >&2; exit 2 ;;
    -h|--help) sed -n '2,23p' "${BASH_SOURCE[0]}"; exit 0 ;;
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

echo "[artifact-synced] 产物已同步；LIVE_ACTIVATION_UNPROVEN：请核对同 PID 的 Host 行与页面功能，勿重启 Host"
