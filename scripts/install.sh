#!/usr/bin/env bash
# Argo 一键安装脚本（克隆/更新真源 + 依赖 + 可选 Skill 链接）
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash
#   ARGO_HOME=/path/to/argo bash scripts/install.sh
#   bash scripts/install.sh --link ~/.claude/skills/argo
#
# 环境变量：
#   ARGO_HOME     安装目录，默认 ~/.local/share/argo
#   ARGO_REPO     仓库地址，默认 https://github.com/taxueseek/argo.git
#   ARGO_BRANCH   分支，默认 main
#   ARGO_SKIP_PIP 设为 1 则跳过 pip 安装
#   ARGO_LINK_TARGETS  冒号分隔的 Skill 入口路径（可选）
#   ARGO_PIN      固定 commit（供应链加固：克隆后校验 HEAD，推荐生产使用）

set -euo pipefail

# 本脚本用 bash 语法（数组、[[ ]]），在 sh/dash/ksh 下会在解析期报一堆语法错误、
# 且看不出真正原因。FreeBSD/OpenBSD/精简容器里 /bin/sh 不是 bash，所以先自证运行
# 环境：不是 bash 就尝试用 bash 重跑，找不到 bash 则给出一句可执行的指引。
if [ -z "${BASH_VERSION:-}" ]; then
  if command -v bash >/dev/null 2>&1; then
    exec bash "$0" "$@"
  fi
  echo "install.sh 需要 bash（本脚本使用数组与 [[ ]] 语法）。" >&2
  echo "  Debian/Ubuntu: sudo apt-get install -y bash" >&2
  echo "  Alpine:        apk add bash" >&2
  echo "  FreeBSD:       pkg install -y bash" >&2
  echo "  OpenBSD:       pkg_add bash" >&2
  echo "  macOS/多数 Linux 自带 bash，可直接： curl -fsSL <url> | bash" >&2
  exit 1
fi

REPO="${ARGO_REPO:-https://github.com/taxueseek/argo.git}"
BRANCH="${ARGO_BRANCH:-main}"
PIN="${ARGO_PIN:-}"
HOME_DIR="${HOME:-$(eval echo ~)}"
INSTALL_DIR="${ARGO_HOME:-$HOME_DIR/.local/share/argo}"
SKIP_PIP="${ARGO_SKIP_PIP:-0}"

LINK_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --home)
      INSTALL_DIR="$2"
      shift 2
      ;;
    --link)
      LINK_ARGS+=(--to "$2")
      shift 2
      ;;
    --skip-pip)
      SKIP_PIP=1
      shift
      ;;
    --pin)
      PIN="$2"
      shift 2
      ;;
    -h|--help)
      sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      exit 1
      ;;
  esac
done

echo "==> Argo 安装目录: $INSTALL_DIR"
echo "==> 仓库来源: $REPO (分支 $BRANCH)"
echo "    提示: 若以 curl|bash 方式执行，请确认来源为 trusted 仓库；生产环境建议 ARGO_PIN 固定 commit。"

if ! command -v git >/dev/null 2>&1; then
  echo "需要 git，请先安装后再试。" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "需要 Python 3.9+，请先安装后再试。" >&2
  exit 1
fi

# 版本下限与 bin/argo 的 MIN_PYTHON 必须一致：改一处就要改另一处。
# 为什么是 3.9：唯一的必需第三方依赖 PyYAML 早已支持 3.9，脚本也几乎都带
# `from __future__ import annotations`；早期这里写 3.10 的理由是「用了 X | None
# 语法」，但实测全仓 0 处该写法。抬高门槛只会把 macOS 原生 python3、老 Debian
# 和 python:3.9 容器这些环境挡在门外。
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 9) else 0)')
if [[ "$PY_OK" != "1" ]]; then
  echo "当前 Python 为 ${PY_VER}，需要 3.9+。" >&2
  exit 1
fi

if [[ -d "$INSTALL_DIR/.git" ]]; then
  echo "==> 已有仓库，拉取更新 ($BRANCH)…"
  git -C "$INSTALL_DIR" fetch --depth 1 origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH" || true
elif [[ -d "$INSTALL_DIR" ]] && [[ -f "$INSTALL_DIR/scripts/search.py" ]]; then
  echo "==> 目录已存在且含 Argo 源码，跳过克隆: $INSTALL_DIR"
else
  echo "==> 克隆仓库…"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  if ! git clone --depth 1 --branch "$BRANCH" "$REPO" "$INSTALL_DIR"; then
    if [[ "$REPO" == *"github.com"* ]]; then
      MIRROR_REPO="https://ghfast.top/${REPO}"
      echo "[warn] 从 GitHub 直连克隆失败，尝试加速镜像: $MIRROR_REPO …"
      if ! git clone --depth 1 --branch "$BRANCH" "$MIRROR_REPO" "$INSTALL_DIR"; then
        echo "!! 克隆失败。若处于受限网络环境，可尝试手动克隆或设置 ARGO_REPO 环境变量。" >&2
        exit 1
      fi
    else
      echo "!! 克隆失败，请检查网络连接。" >&2
      exit 1
    fi
  fi
fi

# 供应链加固：校验 HEAD 与预期 commit 一致（ARGO_PIN 固定）
if [[ -n "$PIN" ]]; then
  ACTUAL_HEAD=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
  if [[ "$ACTUAL_HEAD" != "$PIN" ]]; then
    echo "!! 供应链校验失败：固定 commit 为 ${PIN}，实际 HEAD 为 ${ACTUAL_HEAD}" >&2
    echo "!! 仓库内容与预期不一致，拒绝继续。请人工核查仓库来源。" >&2
    exit 1
  fi
  echo "==> 供应链校验通过: HEAD=$PIN"
fi

if [[ "$SKIP_PIP" != "1" ]]; then
  # 依赖优先装进 $INSTALL_DIR/.venv（bin/argo 的解释器候选链会自动发现它，
  # 见 _venv_python）。为什么要 venv：PEP 668（Homebrew、Debian 12+、
  # Ubuntu 23.04+）把系统 Python 标记为 externally-managed，`pip install
  # --user` 与全局安装都会被拒——旧的三段回落（--user → 全局 → 清华镜像）
  # 在这类机器上三连败，装完没有 PyYAML，第一次 `argo search` 就倒在
  # ModuleNotFoundError（2026-10-23 审计实测）。venv 是解释器自己管的目录，
  # 不受该限制，也不污染用户 site-packages（user-site 遮蔽 Homebrew 主树
  # 的历史风险一并消失）。
  #
  # 2026-11-09 收口：有 uv 时走 `uv sync --frozen --group dev`——解释器版本读
  # 仓内 .python-version、依赖版本读 uv.lock，装出来的环境与 CI 完全一致。
  # uv 不可用时才回落到 python3 -m venv + pip（旧路径保留，Windows 与精简
  # 容器可能没有 uv）。
  VENV_DIR="$INSTALL_DIR/.venv"
  if command -v uv >/dev/null 2>&1; then
    # 解释器版本必须显式传 --python，不能只靠 .python-version：
    # 环境变量 UV_PYTHON 会覆盖 .python-version（实测：本机 UV_PYTHON 指向
    # 3.15 时，`uv venv --python 3.14` 建对了，随后不带 --python 的 `uv sync`
    # 把 .venv 换成了 3.15）。CI 里没有这个变量，本机开发有——同一份脚本在
    # 两处跑出不同版本，正是「版本一致性」要消掉的形态。
    ARGO_PINNED_PY="$(cat "$INSTALL_DIR/.python-version" 2>/dev/null || echo 3.12)"
    echo "==> 用 uv 按 .python-version（${ARGO_PINNED_PY}）+ uv.lock 同步环境…"
    if uv venv "$VENV_DIR" --python "$ARGO_PINNED_PY" >/dev/null 2>&1 \
       && uv sync --frozen --group dev --python "$ARGO_PINNED_PY" >/dev/null 2>&1; then
      echo "==> 依赖就绪（解释器: $VENV_DIR/bin/python，版本 $("$VENV_DIR/bin/python" --version 2>&1)）"
    else
      echo "[warn] uv sync 失败，回退 python3 -m venv + pip…"
      rm -rf "$VENV_DIR"
      _argo_venv_pip_fallback=1
    fi
  else
    _argo_venv_pip_fallback=1
  fi
  if [[ "${_argo_venv_pip_fallback:-0}" == "1" ]]; then
    if python3 -m venv "$VENV_DIR" 2>/dev/null; then
      echo "==> 创建虚拟环境: ${VENV_DIR}（依赖装在这里，不碰系统 Python）"
      "$VENV_DIR/bin/pip" install -q pyyaml \
        || echo "[warn] venv 内 PyYAML 安装失败，可手动: $VENV_DIR/bin/pip install pyyaml"
      echo "==> 安装可选增强 (curl_cffi: TLS 指纹伪造；rapidfuzz: 去重模糊近重复复核；缺失不影响核心功能)…"
      "$VENV_DIR/bin/pip" install -q curl_cffi rapidfuzz 2>/dev/null \
        || echo "[warn] 可选增强安装失败（不影响核心功能）；需要时: $VENV_DIR/bin/pip install curl_cffi rapidfuzz"
    else
      echo "[warn] venv 创建失败（Debian/Ubuntu 需先 sudo apt install python3-venv），回退系统 pip 安装…"
      python3 -m pip install --user -q pyyaml 2>/dev/null \
        || python3 -m pip install -q pyyaml 2>/dev/null \
        || python3 -m pip install --user -q -i https://pypi.tuna.tsinghua.edu.cn/simple pyyaml 2>/dev/null \
        || echo "[warn] pip 安装 PyYAML 失败，可手动: pip install pyyaml"
      echo "==> 安装可选增强 (curl_cffi: TLS 指纹伪造；rapidfuzz: 去重模糊近重复复核；缺失不影响核心功能)…"
      python3 -m pip install --user -q curl_cffi 2>/dev/null \
        || python3 -m pip install -q curl_cffi 2>/dev/null \
        || python3 -m pip install --user -q -i https://pypi.tuna.tsinghua.edu.cn/simple curl_cffi 2>/dev/null \
        || echo "[warn] pip 安装 curl_cffi 失败（可选依赖）。需要反爬 TLS 指纹伪造时手动: pip install curl_cffi"
      python3 -m pip install --user -q rapidfuzz 2>/dev/null \
        || python3 -m pip install -q rapidfuzz 2>/dev/null \
        || python3 -m pip install --user -q -i https://pypi.tuna.tsinghua.edu.cn/simple rapidfuzz 2>/dev/null \
        || echo "[warn] pip 安装 rapidfuzz 失败（可选依赖）。缺失时去重回退纯 minhash，需要模糊近重复复核时手动: pip install rapidfuzz"
    fi
  fi
fi

# ── pre-commit 快门禁 hook（2026-10-16 流程洞实证后新增，2026-11-08 改调度器）──
# 背景：并发代理接线没跑测试，6 道门禁红 + 阻塞全量收集。拦截点放在提交层。
#
# 2026-11-08 改法：旧版把 scripts/pre-commit 拷成 hooks/pre-commit-argo，再往既有
# hook 末尾追一行调用。两处都会静默失效——既有 hook 以 `exit 0` 结尾时追加行永远
# 跑不到（本机 privacy-guard 正是如此，10-17 到 11-08 门禁一次没跑过）；拷贝件也
# 不随仓内门禁更新。现在 hooks/pre-commit 是一个调度器：先跑用户原有 hook（原样
# 改名为 pre-commit.user，内容不动），失败即中止；再直接跑仓内 scripts/pre-commit。
if command -v git >/dev/null 2>&1 && { [ -d "$INSTALL_DIR/.git" ] || [ -f "$INSTALL_DIR/.git" ]; }; then
  _common=$(cd "$INSTALL_DIR" && git rev-parse --git-common-dir 2>/dev/null || true)
  case "$_common" in
    /*) ;;
    "") _common="" ;;
    *)  _common="$INSTALL_DIR/$_common" ;;
  esac
  if [ -n "$_common" ] && [ -d "$_common" ]; then
    _hooks_dir="$_common/hooks"
    _hook="$_hooks_dir/pre-commit"
    _marker="# argo-hook-dispatcher v2"
    mkdir -p "$_hooks_dir"
    if [ -f "$_hook" ] && grep -qF "$_marker" "$_hook"; then
      echo "==> pre-commit 调度器已就位（无需重装）"
    else
      if [ -f "$_hook" ] && { grep -qF "# argo-managed-hook" "$_hook" || grep -q "^# argo 提交前快门禁" "$_hook"; }; then
        # argo 自产 hook（新版带 argo-managed-hook 标记；旧版 cp 件头行是「argo 提交
        # 前快门禁」——2026-11-08 审查修复：旧模式串与真实头行不符，分支永不命中，
        # 旧门禁整块溜进 pre-commit.user 每次 commit 双跑）：不是用户自定义 hook，直接替换
        rm -f "$_hook"
      elif [ -f "$_hook" ] && ! grep -q "pre-commit-argo" "$_hook"; then
        mv "$_hook" "$_hooks_dir/pre-commit.user"
      elif [ -f "$_hook" ]; then
        # 旧版向用户 hook 末尾追加过调用行：安全剥离那两行（防 pipefail 因无剩余行退出）
        { grep -v -e "pre-commit-argo" -e "# argo 快门禁（2026-10-17 追加" "$_hook" || true; } > "$_hooks_dir/pre-commit.user"
        if grep -qE '^[[:space:]]*[^#[:space:]]' "$_hooks_dir/pre-commit.user"; then
          chmod +x "$_hooks_dir/pre-commit.user"
        else
          rm -f "$_hooks_dir/pre-commit.user"
        fi
        rm -f "$_hook"
      fi
      rm -f "$_hooks_dir/pre-commit-argo"
      cat > "$_hook" <<'ARGO_HOOK_EOF'
#!/bin/sh
# argo-hook-dispatcher v2（由 argo scripts/install.sh 生成）
# 1) 用户原有 hook（pre-commit.user）先跑，非 0 即中止
# 2) 再跑仓内 scripts/pre-commit 快门禁（直接调用，不拷贝，始终是当前版本）
here=$(cd "$(dirname "$0")" && pwd)
if [ -x "$here/pre-commit.user" ]; then
  "$here/pre-commit.user" "$@" || exit $?
fi
root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 1
if [ -f "$root/scripts/pre-commit" ]; then
  exec sh "$root/scripts/pre-commit" "$@"
fi
exit 0
ARGO_HOOK_EOF
      chmod +x "$_hook"
      echo "==> 已装 pre-commit 调度器（原有 hook 保留为 pre-commit.user；git commit --no-verify 可跳过）"
    fi
  fi
fi


if [[ ${#LINK_ARGS[@]} -gt 0 ]] || [[ -n "${ARGO_LINK_TARGETS:-}" ]] || [[ -f "$INSTALL_DIR/installs.local.yaml" ]]; then
  echo "==> 链接 Skill 入口（符号链接回真源，不复制）…"
  python3 "$INSTALL_DIR/scripts/link_source.py" "${LINK_ARGS[@]+"${LINK_ARGS[@]}"}" || {
    echo "[warn] 链接未完成。可稍后手动:"
    echo "  python3 $INSTALL_DIR/scripts/link_source.py --to ~/.claude/skills/argo"
  }
fi

# 文档里的解释器：venv 在就用它（与 bin/argo 的选择一致，避免「装好了但
# 示例跑不起来」——系统 python3 可能没有 PyYAML）。
RUN_PY="python3"
if [[ -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  RUN_PY="$INSTALL_DIR/.venv/bin/python"
fi

echo ""
echo "==> 安装完成"
echo ""
echo "快速验证:"
echo "  $RUN_PY $INSTALL_DIR/scripts/search.py \"Python asyncio\" --json"
echo "  $RUN_PY $INSTALL_DIR/scripts/search.py --list-engines"
echo ""
echo "启动 MCP（给 Claude / Kimi / Cursor 等用）:"
echo "  $RUN_PY $INSTALL_DIR/scripts/mcp_server.py"
echo ""
echo "或用 npx（需 Node.js 18+）:"
echo "  npx -y github:taxueseek/argo"
echo ""
echo "客户端 MCP 配置示例（路径请按本机替换）:"
cat <<EOF
{
  "mcpServers": {
    "argo": {
      "command": "$RUN_PY",
      "args": ["$INSTALL_DIR/scripts/mcp_server.py"]
    }
  }
}
EOF
echo ""
echo "可选：把 Skill 挂到本机 Agent 目录（不复制代码）:"
echo "  $RUN_PY $INSTALL_DIR/scripts/link_source.py --to ~/.claude/skills/argo"
echo "  $RUN_PY $INSTALL_DIR/scripts/link_source.py --to ~/.agents/skills/argo"
echo ""
echo "文档: https://github.com/taxueseek/argo"
