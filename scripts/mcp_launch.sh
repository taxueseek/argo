#!/bin/sh
# argo MCP 启动器 —— 保证任何客户端、任何启动上下文都拿到引擎密钥。
#
# 背景（2026-08-29）：各客户端 server 的密钥来自「宿主 app 启动时的环境」，
# 上下文不同密钥就不同（有的 8 个全有、有的全无），且 launchctl setenv
# 重启即失效。此启动器把密钥收敛到唯一真源文件，启动时注入：
#   1. ~/.config/argo/env（chmod 600，勿入库勿分享）——唯一真源
#   2. 系统域兜底：macOS 走 launchctl getenv，其它平台无此层、直接跳过
# 提前恢复配额/换 key：改 env 文件后重启对应客户端即可，零配置分散维护。
#
# 跨平台（2026-09-15）：原版是 `#!/bin/zsh` + `dscl` + `${(P)k}` + `${0:A:h}`
# + 写死 `/opt/homebrew/bin/python3`，只在装了 Homebrew 的 macOS 上成立。
# 而 `mcp_setup.py` 把本脚本路径写成 MCP 客户端的 command（10 处引用），
# 于是 Linux / Windows-WSL 用户接 MCP 的第一步就是
# `bad interpreter: /bin/zsh`——不是配置错，是脚本根本起不来。
# 改用 POSIX sh + 解释器探测后，macOS 本机行为不变（同一个 env 文件、同一层
# launchctl 兜底、同一批透传名单），其余平台从「拉不起来」变成「能起」。

set -a

# 某些宿主（如 dsh web）以净化环境 spawn MCP，HOME 可能缺失——先兜底
if [ -z "$HOME" ]; then
  if command -v dscl >/dev/null 2>&1; then                 # macOS
    HOME="$(dscl . -read "/Users/$(id -un)" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
  elif command -v getent >/dev/null 2>&1; then             # Linux
    HOME="$(getent passwd "$(id -un)" | cut -d: -f6)"
  fi
  [ -n "$HOME" ] && export HOME
fi

ENV_FILE="${HOME}/.config/argo/env"
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

# 透传名单单一真源：launchctl print 批量路径与 getenv 回退路径共用这一份
# （与 engine_env.KNOWN_ENV_ALIASES 展开 + 代理变量对齐，
# tests/test_launch_passthrough_alignment.py 锁两表一致，改名单两处同步）
_ARGO_PASSTHROUGH_KEYS="ALL_PROXY ANYSEARCH_API_KEY ARGO_ANYSEARCH_API_KEY ARGO_BOCHA_API_KEY\
 ARGO_BRAVE_API_KEY ARGO_BYTED_API_KEY ARGO_EASTMONEY_APIKEY\
 ARGO_EXA_API_KEY ARGO_FELO_API_KEY ARGO_FIRECRAWL_API_KEY\
 ARGO_GITHUB_TOKEN ARGO_METASO_API_KEY ARGO_OCTEN_API_KEY\
 ARGO_PROXY ARGO_QWEATHER_KEY ARGO_TAVILY_API_KEY\
 ARGO_WEB_SEARCH_API_KEY ARGO_WEREAD_API_KEY\
 ARGO_WOLFRAM_APPID ARGO_ZHIHU_ACCESS_SECRET BOCHA_API_KEY\
 BRAVE_API_KEY EASTMONEY_APIKEY EXA_API_KEY FELO_API_KEY\
 FIRECRAWL_API_KEY GITHUB_TOKEN HTTPS_PROXY HTTP_PROXY\
 METASO_API_KEY NO_PROXY OCTEN_API_KEY QWEATHER_KEY\
 TAVILY_API_KEY WEB_SEARCH_API_KEY WEREAD_API_KEY\
 WOLFRAM_APPID ZHIHU_ACCESS_SECRET"

# launchctl 兜底（macOS）：旧实现逐键 spawn `launchctl getenv`——实测每次
# 4-20ms，名单里 ~30 个未设键让每个宿主会话白付百毫秒级纯开销。改为一次
# `launchctl print gui/$UID` 读整个域环境（实测热态 ~30ms）：environment 段
# 的 "KEY => value" 行与 getenv 同源（setenv 探针实测，含空格值一致）。
# print 不可用（非 macOS / 特殊上下文）时回退旧逐键 getenv，行为同 08-29 版。
_lc_vals=""
if command -v launchctl >/dev/null 2>&1; then
  _lc_vals="$(launchctl print "gui/$(id -u)" 2>/dev/null | LC_ALL=C awk -v keys="$_ARGO_PASSTHROUGH_KEYS" '
    BEGIN { n = split(keys, a, " "); for (i = 1; i <= n; i++) want[a[i]] = 1 }
    /^[[:space:]]*environment[[:space:]]*=[[:space:]]*\{/ { e = 1; next }
    e && /^[[:space:]]*\}/ { exit }
    e && / => / {
      i = index($0, " => ")
      k = substr($0, 1, i - 1)
      sub(/^[[:space:]]+/, "", k)
      if (k in want) print k "\t" substr($0, i + 4)
    }')"
fi
if [ -n "$_lc_vals" ]; then
  # 名单内已设的键逐条导出；env 文件/父环境已给的键不覆盖（语义同旧循环）
  while IFS="	" read -r k v; do
    eval "cur=\${$k}"
    if [ -z "$cur" ] && [ -n "$v" ]; then
      export "$k=$v"
    fi
  done <<_ARGO_LC_EOF
$_lc_vals
_ARGO_LC_EOF
else
  for k in $_ARGO_PASSTHROUGH_KEYS; do
    if command -v launchctl >/dev/null 2>&1; then
      # POSIX 间接展开（zsh 的 ${(P)k} 在 sh 下不存在，eval 是等价写法）
      eval "cur=\${$k}"
      if [ -z "$cur" ]; then
        v="$(launchctl getenv "$k" 2>/dev/null)"
        [ -n "$v" ] && export "$k=$v"
      fi
    fi
  done
fi
set +a

# 脚本所在目录的绝对路径（POSIX 写法，替代 zsh 的 ${0:A:h}）
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

# 解释器：ARGO_PYTHON 显式指定优先（客户端配置可覆盖）；否则自动探测，取第一个
# 同时满足「版本 ≥3.9」且「能导入 PyYAML」的——版本下限与 bin/argo 的 MIN_PYTHON
# 一致（改一处就要改另一处）。为什么不是更高的版本：早期这里写 3.10，理由是
# 「脚本用了 `X | None`」，但实测全仓 0 处该写法。而配置/引擎声明都靠 PyYAML
# 解析，版本够却没装 yaml 的解释器选了也只能用残缺的内置默认配置——所以这两点
# 必须**同时**判，少判一个就会选到跑不动的解释器（bin/argo 曾因此把 3.9 选中
# 并写进缓存）。报错最终都落在客户端日志里，用户看到的是「MCP server 未就绪」，
# 所以探测阶段就把不可用的解释器跳过。
# 全都不达标时回落 python3：宁可让错误信息可读，也不抛「找不到解释器」。
# ARGO_PYTHON 显式指定同样要过「版本 + PyYAML」双判据（2026-10-02 补齐）：
# 此前只查存在性，把无 yaml 的解释器写进 env 文件会带着残缺配置起服务，
# 宿主只看到「MCP server 未就绪」——与下方自动探测同一条判据。
if [ -n "$ARGO_PYTHON" ] && command -v "$ARGO_PYTHON" >/dev/null 2>&1 \
   && "$ARGO_PYTHON" -c 'import sys, yaml; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
  PY="$ARGO_PYTHON"
else
  PY=""
  for c in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    if "$p" -c 'import sys, yaml; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
      PY="$p"
      break
    fi
  done
  [ -n "$PY" ] || PY="python3"
fi

exec "$PY" -u "${SCRIPT_DIR}/mcp_server.py" "$@"
