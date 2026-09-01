#!/bin/zsh
# argo MCP 启动器 — 保证任何客户端、任何启动上下文都拿到引擎密钥。
#
# 背景（2026-08-29）：各客户端 server 的密钥来自「宿主 app 启动时的环境」，
# 上下文不同密钥就不同（有的 8 个全有、有的全无），且 launchctl setenv
# 重启即失效。此启动器把密钥收敛到唯一真源文件，启动时注入：
#   1. ~/.config/argo/env（chmod 600，勿入库勿分享）——唯一真源
#   2. launchctl getenv 兜底（该文件缺失时仍可能从系统域拿到）
# 提前恢复配额/换 key：改 env 文件后重启对应客户端即可，零配置分散维护。

set -a
# 某些宿主（如 dsh web）以净化环境 spawn MCP，HOME 可能缺失——先兜底
if [ -z "$HOME" ]; then
  export HOME="$(dscl . -read "/Users/$(id -un)" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
fi
ENV_FILE="${HOME}/.config/argo/env"
[ -f "$ENV_FILE" ] && source "$ENV_FILE"

for k in ZHIHU_ACCESS_SECRET BOCHA_API_KEY EXA_API_KEY OCTEN_API_KEY \
         TAVILY_API_KEY WEB_SEARCH_API_KEY ANYSEARCH_API_KEY WEREAD_API_KEY \
         TINYFISH_API_KEY; do
  if [ -z "${(P)k}" ]; then
    v=$(launchctl getenv "$k" 2>/dev/null)
    [ -n "$v" ] && export "$k=$v"
  fi
done
set +a

SCRIPT_DIR="${0:A:h}"
exec "${ARGO_PYTHON:-/opt/homebrew/bin/python3}" -u "${SCRIPT_DIR}/mcp_server.py" "$@"
