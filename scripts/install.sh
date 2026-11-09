#!/usr/bin/env bash
# Argo 安装脚本 —— 克隆/更新真源、装依赖、挂 Skill 入口、装提交门禁。
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/taxueseek/argo/main/scripts/install.sh | bash
#   ARGO_HOME=/path/to/argo bash scripts/install.sh
#   bash scripts/install.sh --link ~/.claude/skills/argo
#   bash scripts/install.sh --dry-run          # 只打印将执行的动作
#   bash scripts/install.sh --uninstall        # 清理本脚本装的东西
#
# 环境变量：
#   ARGO_HOME           安装目录，默认 ~/.local/share/argo
#   ARGO_REPO           仓库地址，默认 https://github.com/taxueseek/argo.git
#   ARGO_BRANCH         分支，默认 main
#   ARGO_SKIP_PIP       设为 1 则跳过依赖安装
#   ARGO_LINK_TARGETS   冒号分隔的 Skill 入口路径（可选）
#   ARGO_PIN            固定 commit（供应链加固：克隆后校验 HEAD）
#
# 设计原则（对应 Agent Skills 规范对 scripts/ 的三条要求）：
#   1. 自包含：单文件、零外部依赖（除 git / python3 / uv 这三件可选工具）；
#   2. 错误信息可执行：每个失败点都给出「该装什么」或「该改哪个变量」；
#   3. 边界优雅：已有仓库、已有链接、已有 hook、缺 uv、PEP 668 环境各自有明确出路。

set -euo pipefail

# 本脚本用 bash 语法（数组、[[ ]]）。FreeBSD/OpenBSD/精简容器里 /bin/sh 不是 bash，
# 直接跑会在解析期报一堆看不出原因的语法错误，所以先自证运行环境。
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

# ── 输出与错误 ────────────────────────────────────────────────────────────────
# 统一出口：info（步骤）、ok（成功）、warn（可继续的失败）、die（中止）。
# die 的每条消息都必须可执行——「该装什么」或「该改哪个变量」，不留「出错了」。
info() { printf '==> %s\n' "$*"; }
ok()   { printf '    [ok] %s\n' "$*"; }
warn() { printf '    [warn] %s\n' "$*" >&2; }
die()  { printf '    [fail] %s\n' "$*" >&2; exit 1; }

# ── 参数与默认值 ──────────────────────────────────────────────────────────────
REPO="${ARGO_REPO:-https://github.com/taxueseek/argo.git}"
BRANCH="${ARGO_BRANCH:-main}"
PIN="${ARGO_PIN:-}"
HOME_DIR="${HOME:-$(eval echo ~)}"
INSTALL_DIR="${ARGO_HOME:-$HOME_DIR/.local/share/argo}"
SKIP_PIP="${ARGO_SKIP_PIP:-0}"
DRY_RUN=0
UNINSTALL=0
LINK_ARGS=()

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home)     INSTALL_DIR="$2"; shift 2 ;;
    --link)     LINK_ARGS+=(--to "$2"); shift 2 ;;
    --skip-pip) SKIP_PIP=1; shift ;;
    --pin)      PIN="$2"; shift 2 ;;
    --dry-run)  DRY_RUN=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *) die "未知参数: $1（--help 看用法）" ;;
  esac
done

# dry-run 下所有会改系统的**简单命令**都只打印。
# 注意：不要用它包「内部还有判断逻辑的函数」——被包的函数在 dry-run 时根本不执行，
# 其内部状态（local 变量、副作用）全部跳过，反而制造假失败。函数自己处理 dry-run。
run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '    [dry-run] %s\n' "$*"
    return 0
  fi
  "$@"
}

# ── 前置检查 ──────────────────────────────────────────────────────────────────
command -v git >/dev/null 2>&1 || die "需要 git。Debian/Ubuntu: sudo apt-get install -y git；macOS: xcode-select --install"
command -v python3 >/dev/null 2>&1 || die "需要 Python 3.9+。python.org 下载，或 pyenv install 3.12"

# 版本下限与 bin/argo 的 MIN_PYTHON、install.ps1 三处必须一致。为什么是 3.9：
# 唯一必需依赖 PyYAML 早已支持 3.9，脚本几乎都带 `from __future__ import annotations`；
# 抬高门槛只会把 macOS 原生 python3.9、老 Debian、python:3.9 容器挡在门外。
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "当前 Python 为 ${PY_VER}，需要 3.9+。装新版本后重跑本脚本即可（不会动系统 Python）"

# ── 卸载 ──────────────────────────────────────────────────────────────────────
if [[ "$UNINSTALL" == "1" ]]; then
  info "卸载 Argo（安装目录 ${INSTALL_DIR}）"
  # 摘 Skill 链接：link_source.py 没有 --undo，手动删指向真源的符号链接。
  # 只删符号链接（-L 判定），绝不碰真实目录——误删用户数据的代价不可逆。
  for _target in "$HOME_DIR/.claude/skills/argo" "$HOME_DIR/.agents/skills/argo" \
                 "$HOME_DIR/.grok/skills/argo" "$HOME_DIR/.codex/skills/argo"; do
    if [[ -L "$_target" && "$(readlink "$_target")" == "$INSTALL_DIR" ]]; then
      run rm -f "$_target"
      ok "已摘除链接 $_target"
    fi
  done
  # 摘 hook 调度器：保留用户的 pre-commit.user
  _common=$(cd "$INSTALL_DIR" 2>/dev/null && git rev-parse --git-common-dir 2>/dev/null || true)
  case "$_common" in
    /*) _hooks="$_common/hooks" ;;
    "") _hooks="" ;;
    *)  _hooks="$INSTALL_DIR/$_common/hooks" ;;
  esac
  if [[ -n "$_hooks" && -f "$_hooks/pre-commit" ]] && grep -qF "# argo-hook-dispatcher v2" "$_hooks/pre-commit"; then
    run rm -f "$_hooks/pre-commit"
    run rm -f "$_hooks/pre-commit-argo"
    ok "已摘除 pre-commit 调度器"
  fi
  # 最后删目录
  if [[ -d "$INSTALL_DIR" ]]; then
    run rm -rf "$INSTALL_DIR"
    ok "已删除 $INSTALL_DIR"
  fi
  info "卸载完成。Skill 目录里的 argo 符号链接如仍存在，手动 rm 即可。"
  exit 0
fi

info "安装目录: $INSTALL_DIR"
info "仓库来源: $REPO (分支 $BRANCH)"
[[ -n "$PIN" ]] && info "供应链固定: commit $PIN"
[[ "$DRY_RUN" == "1" ]] && warn "dry-run 模式：只打印动作，不实际执行"
echo "    提示: 若以 curl|bash 执行，请确认来源可信；生产环境建议 ARGO_PIN 固定 commit。"

# ── 1. 克隆 / 更新真源 ────────────────────────────────────────────────────────
if [[ -d "$INSTALL_DIR/.git" ]]; then
  info "已有仓库，拉取更新 ($BRANCH)…"
  run git -C "$INSTALL_DIR" fetch --depth 1 origin "$BRANCH"
  run git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH" 2>/dev/null || warn "pull 失败（可能有本地改动），继续使用当前版本"
elif [[ -d "$INSTALL_DIR" && -f "$INSTALL_DIR/scripts/search.py" ]]; then
  info "目录已存在且含 Argo 源码，跳过克隆: $INSTALL_DIR"
else
  info "克隆仓库…"
  run mkdir -p "$(dirname "$INSTALL_DIR")"
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '    [dry-run] git clone --depth 1 --branch %s %s %s\n' "$BRANCH" "$REPO" "$INSTALL_DIR"
  elif ! git clone --depth 1 --branch "$BRANCH" "$REPO" "$INSTALL_DIR"; then
    if [[ "$REPO" == *"github.com"* ]]; then
      MIRROR_REPO="https://ghfast.top/${REPO}"
      warn "从 GitHub 直连克隆失败，尝试加速镜像: $MIRROR_REPO"
      git clone --depth 1 --branch "$BRANCH" "$MIRROR_REPO" "$INSTALL_DIR" \
        || die "克隆失败（含镜像）。受限网络可：git clone --depth 1 $REPO $INSTALL_DIR 后重跑本脚本"
    else
      die "克隆失败。检查网络，或设置 ARGO_REPO 指向可达镜像"
    fi
  fi
  ok "仓库就绪"
fi

# 供应链加固：校验 HEAD 与预期 commit 一致（ARGO_PIN 固定时）
if [[ -n "$PIN" ]]; then
  ACTUAL_HEAD=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
  [[ "$ACTUAL_HEAD" == "$PIN" ]] || die "供应链校验失败：固定 commit 为 ${PIN}，实际 HEAD 为 ${ACTUAL_HEAD}。仓库内容与预期不一致，拒绝继续"
  ok "供应链校验通过: HEAD=$PIN"
fi

# ── 2. 依赖安装 ───────────────────────────────────────────────────────────────
# 装进 $INSTALL_DIR/.venv（bin/argo 的解释器候选链会自动发现它，见 _venv_python）。
# 为什么必须 venv：PEP 668（Homebrew、Debian 12+、Ubuntu 23.04+）把系统 Python
# 标记为 externally-managed，`pip install --user` 与全局安装都会被拒。venv 是解释器
# 自己管的目录，不受该限制，也不污染用户 site-packages。
#
# 两条路径，优先 uv（解释器版本读 .python-version、依赖读 uv.lock，与 CI 完全一致）：
#   有 uv：uv venv + uv sync --frozen --group dev
#   无 uv：python3 -m venv + pip install（回落）
pinned_python() {
  local v="3.12"
  if [[ -f "$INSTALL_DIR/.python-version" ]]; then
    read -r v < "$INSTALL_DIR/.python-version" || v="3.12"
    v="${v//$'\r'/}"
    v="${v//$'\n'/}"
  fi
  printf '%s' "$v"
}

install_deps_with_uv() {
  local venv_dir="$INSTALL_DIR/.venv"
  local pinned
  pinned="$(pinned_python)"
  info "用 uv 按 .python-version（${pinned}）+ uv.lock 同步环境…"
  # 显式传 --python，不能只靠 .python-version：环境变量 UV_PYTHON 会覆盖它
  # （实测本机 UV_PYTHON 指向 3.15 时，建对 3.14 的 venv 后 sync 会换成 3.15）。
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '    [dry-run] uv venv %s --python %s\n' "$venv_dir" "$pinned"
    printf '    [dry-run] uv sync --frozen --group dev --python %s\n' "$pinned"
    return 0
  fi
  # venv 已存在时 uv venv 会直接报错退出（实测 exit=2，提示加 --clear）。
  # 安装场景下 venv 存在是常态（用户重跑安装），不该报错——存在就跳过创建，
  # 交给 uv sync 补齐（sync 对已存在的 venv 是增量更新）。
  if [[ ! -x "$venv_dir/bin/python" ]]; then
    uv venv "$venv_dir" --python "$pinned" >/dev/null 2>&1 || return 1
  fi
  uv sync --frozen --group dev --python "$pinned" >/dev/null 2>&1 || return 1
  # uv sync 的盲区：它只对照 lock 与 venv 的「已装记录」，不对照磁盘真实状态。
  # 手工 `pip uninstall pyyaml` 后 uv sync 报「Checked 17 packages」但包仍缺
  # （实测）。所以装完必须真 import 一次，缺则 --reinstall 强制重建。
  if ! "$venv_dir/bin/python" -c 'import yaml' >/dev/null 2>&1; then
    warn "uv sync 未补齐依赖（venv 记录与磁盘不一致），强制重装…"
    uv sync --frozen --group dev --python "$pinned" --reinstall >/dev/null 2>&1 || return 1
  fi
  ok "依赖就绪（解释器 $("$venv_dir/bin/python" --version 2>&1)）"
}

install_deps_with_pip() {
  local venv_dir="$INSTALL_DIR/.venv"
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '    [dry-run] python3 -m venv %s\n' "$venv_dir"
    printf '    [dry-run] %s/bin/pip install pyyaml curl_cffi rapidfuzz\n' "$venv_dir"
    return 0
  fi
  if python3 -m venv "$venv_dir" 2>/dev/null; then
    info "创建虚拟环境: ${venv_dir}（依赖装在这里，不碰系统 Python）"
    "$venv_dir/bin/pip" install -q pyyaml \
      || warn "venv 内 PyYAML 安装失败，可手动: $venv_dir/bin/pip install pyyaml"
    info "安装可选增强 (curl_cffi: TLS 指纹伪造；rapidfuzz: 去重模糊近重复复核)…"
    "$venv_dir/bin/pip" install -q curl_cffi rapidfuzz 2>/dev/null \
      || warn "可选增强安装失败（不影响核心功能）；需要时手动: $venv_dir/bin/pip install curl_cffi rapidfuzz"
    ok "依赖就绪（解释器 $("$venv_dir/bin/python" --version 2>&1)）"
  else
    warn "venv 创建失败（Debian/Ubuntu 需先 sudo apt install python3-venv），回退系统 pip 安装…"
    # PEP 668 下这段大概率三连败，但保留给非 PEP 668 的老系统。
    python3 -m pip install --user -q pyyaml 2>/dev/null \
      || python3 -m pip install -q pyyaml 2>/dev/null \
      || python3 -m pip install --user -q -i https://pypi.tuna.tsinghua.edu.cn/simple pyyaml 2>/dev/null \
      || warn "pip 安装 PyYAML 失败（PEP 668 环境属预期）。手动出路: python3 -m venv $venv_dir && $venv_dir/bin/pip install pyyaml"
    python3 -m pip install --user -q curl_cffi 2>/dev/null || python3 -m pip install -q curl_cffi 2>/dev/null || true
    python3 -m pip install --user -q rapidfuzz 2>/dev/null || python3 -m pip install -q rapidfuzz 2>/dev/null || true
  fi
}

if [[ "$SKIP_PIP" != "1" ]]; then
  if command -v uv >/dev/null 2>&1; then
    install_deps_with_uv || { warn "uv sync 失败，回退 python3 -m venv + pip…"; rm -rf "$INSTALL_DIR/.venv"; install_deps_with_pip; }
  else
    install_deps_with_pip
  fi
fi

# ── 3. pre-commit 门禁 hook ───────────────────────────────────────────────────
# 调度器形态（2026-11-08 改）：hooks/pre-commit 先跑用户原有 hook（pre-commit.user，
# 内容不动），失败即中止；再直接跑仓内 scripts/pre-commit（不拷贝，始终是当前版本）。
# 旧版两种形态都会静默失效：追加行在以 exit 0 结尾的 hook 后永远跑不到；拷贝件
# 不随仓内门禁更新。
install_hook_dispatcher() {
  [[ -d "$INSTALL_DIR/.git" || -f "$INSTALL_DIR/.git" ]] || return 0
  local _common _hooks _hook
  _common=$(cd "$INSTALL_DIR" && git rev-parse --git-common-dir 2>/dev/null || true)
  case "$_common" in
    /*) _hooks="$_common/hooks" ;;
    "") return 0 ;;
    *)  _hooks="$INSTALL_DIR/$_common/hooks" ;;
  esac
  [[ -d "$_hooks" ]] || return 0
  _hook="$_hooks/pre-commit"

  if [[ "$DRY_RUN" == "1" ]]; then
    printf '    [dry-run] install pre-commit dispatcher into %s\n' "$_hooks"
    return 0
  fi

  mkdir -p "$_hooks"
  if [[ -f "$_hook" ]] && grep -qF "# argo-hook-dispatcher v2" "$_hook"; then
    ok "pre-commit 调度器已就位（无需重装）"
    return 0
  fi

  # 清理 argo 自产旧 hook：新版带标记；旧版 cp 件头行是「argo 提交前快门禁」。
  # 旧模式串与真实头行不符会导致分支永不命中（2026-11-08 审查修复）。
  if [[ -f "$_hook" ]] && { grep -qF "# argo-managed-hook" "$_hook" || grep -q "^# argo 提交前快门禁" "$_hook"; }; then
    rm -f "$_hook"
  elif [[ -f "$_hook" ]] && ! grep -q "pre-commit-argo" "$_hook"; then
    mv "$_hook" "$_hooks/pre-commit.user"
  elif [[ -f "$_hook" ]]; then
    # 旧版向用户 hook 末尾追加过调用行：剥离那两行，保留其余内容
    { grep -v -e "pre-commit-argo" -e "# argo 快门禁（2026-10-17 追加" "$_hook" || true; } > "$_hooks/pre-commit.user"
    if grep -qE '^[[:space:]]*[^#[:space:]]' "$_hooks/pre-commit.user"; then
      chmod +x "$_hooks/pre-commit.user"
    else
      rm -f "$_hooks/pre-commit.user"
    fi
    rm -f "$_hook"
  fi
  rm -f "$_hooks/pre-commit-argo"

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
  ok "已装 pre-commit 调度器（原有 hook 保留为 pre-commit.user；git commit --no-verify 可跳过）"
}

install_hook_dispatcher

# ── 4. Skill 入口链接 ─────────────────────────────────────────────────────────
# 符号链接回真源，不复制代码——改源码即生效，不产生副本漂移。
if [[ ${#LINK_ARGS[@]} -gt 0 || -n "${ARGO_LINK_TARGETS:-}" || -f "$INSTALL_DIR/installs.local.yaml" ]]; then
  info "链接 Skill 入口（符号链接回真源，不复制）…"
  run python3 "$INSTALL_DIR/scripts/link_source.py" "${LINK_ARGS[@]+"${LINK_ARGS[@]}"}" \
    || warn "链接未完成。可稍后手动: python3 $INSTALL_DIR/scripts/link_source.py --to ~/.claude/skills/argo"
fi

# ── 5. 安装后验证 ─────────────────────────────────────────────────────────────
# 用 venv 的 python 真 import 一次，把「装好了但跑不起来」挡在安装时。
RUN_PY="python3"
[[ -x "$INSTALL_DIR/.venv/bin/python" ]] && RUN_PY="$INSTALL_DIR/.venv/bin/python"
if [[ "$SKIP_PIP" != "1" && -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  if "$INSTALL_DIR/.venv/bin/python" -c 'import yaml' >/dev/null 2>&1; then
    ok "依赖自检通过（PyYAML 可用）"
  else
    warn "venv 内 PyYAML 不可用。手动修复: $INSTALL_DIR/.venv/bin/pip install pyyaml"
  fi
fi

# ── 完成输出 ──────────────────────────────────────────────────────────────────
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
echo "卸载: bash scripts/install.sh --uninstall"
echo "文档: https://github.com/taxueseek/argo"
