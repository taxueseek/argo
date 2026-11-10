#!/usr/bin/env bash
# 构建 Argo Release 资产包（wheel、sdist 与逐个资产的 .sha256 校验文件）
#
# 用法：
#   bash scripts/build_release_assets.sh
#   产物输出至 dist/ 目录。
#
# ## 为什么只打 wheel 与 sdist，不再打运行时源码包
#
# 2026-11-10（v2.14.0 发版后）审视：仓库曾额外打一个 argo-v<ver>.tar.gz
# （运行时子集，不含 tests）。实测同类项目（psf/requests、pandas、
# astral-sh/uv）的 Release 资产都是 wheel + sdist 两件套，没有一个手动传
# 运行时源码包——因为 GitHub 每个 Release 页面**自动生成** Source code
# (tar.gz) 与 (zip)，那是完整仓库快照，「要源码」的需求已被平台覆盖。
#
# 按需求穷举（MECE）：
#   普通用户装 CLI      → npm（主路径）或 wheel
#   想跑 main 最新       → git clone / npx github:...（不经过 Release）
#   要审源码 / 改源码   → GitHub 自动生成的 Source code（平台已覆盖）
#   无 wheel 平台装      → sdist（pip install xxx.tar.gz）
#   想拿运行时文件直接跑 → 没有安装路径引导到这里，属不存在的需求
# 最后一行是那个源码包唯一独有的场景，而它没有触达路径。保留它意味着每版
# 多约 2 MB 下载、多一处版本号同步、多一个可能与 sdist 不一致的副本。
# 故删除。
#
# ## 为什么校验和改成逐个资产一个 .sha256
#
# 照 astral-sh/uv 的格式。合并清单（SHA256SUMS.txt）有两个缺点：校验时
# 必须先下清单再逐项对；清单本身是单点——它若损坏，所有校验都不可用。
# 独立文件可以边下边校，且一个损坏不影响其余。

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# 解释器选择：uv 同步出来的 .venv 优先（与 CI 同一档），否则回落 python3。
# 为什么不用裸 python3：CI 里 .venv 是唯一钉版解释器，脚本若在系统 python3 上
# 跑，`import build` 的判定会随机器而变——同一份代码在不同机器打出不同资产。
PY="$ROOT_DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then
    PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ]; then
    echo "找不到可用的 Python 解释器（需要 .venv/bin/python 或 python3）" >&2
    exit 1
fi

VERSION=$("$PY" -c 'import json; print(json.load(open("package.json"))["version"])')
DIST_DIR="$ROOT_DIR/dist"
# staging 是「wheel 的运行时数据源」：复制到 argo_search/_data 后交给 build，
# 构建完清理（见下）。它不再被打包成独立的运行时源码包。
STAGE_DIR="$DIST_DIR/.stage-argo-$VERSION"

echo "==> 开始构建 Argo Release 资产包 (v$VERSION)..."
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR" "$STAGE_DIR"

# 1. 组织 wheel 的运行时数据（与此前同口径：运行时必需文件，排除测试与缓存）
echo "--> 组织运行时数据..."
cp -r bin config.yaml engines backends data mcp scripts sub-skills README.md SKILL.md LICENSE package.json pyproject.toml AGENTS.md docs "$STAGE_DIR/"

# 清理 staging 内的多余缓存与测试
find "$STAGE_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$STAGE_DIR" -name "*.pyc" -delete 2>/dev/null || true
rm -rf "$STAGE_DIR/data/jobs" 2>/dev/null || true
find "$STAGE_DIR/sub-skills" -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true
find "$STAGE_DIR/sub-skills" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# 2. 打 wheel 与 sdist。
# wheel 的运行树来自 staging：构建前复制到 argo_search/_data，构建后清理。
# 这样 pip/uvx 安装后 console script 仍能找到 config/engines/sub-skills。
if "$PY" -c 'import build' 2>/dev/null; then
    echo "--> 构建 Python Wheel 与 Sdist..."
    PKG_DATA="$ROOT_DIR/argo_search/_data"
    rm -rf "$PKG_DATA"
    cp -r "$STAGE_DIR" "$PKG_DATA"
    "$PY" -m build --outdir "$DIST_DIR"
    rm -rf "$PKG_DATA"
elif "$PY" -c 'import setuptools, wheel' 2>/dev/null; then
    echo "--> 使用 setuptools 构建 Wheel..."
    "$PY" setup.py bdist_wheel --dist-dir "$DIST_DIR" 2>/dev/null || true
fi
rm -rf "$STAGE_DIR"

# 3. 逐个资产生成 .sha256（格式照 astral-sh/uv：`<sha256>  <filename>`，两空格）
# 资产清单由本脚本的产物决定，与 CI 的 `files: dist/*` 同出一源——不再有
# 第二个地方维护清单。
echo "--> 生成逐个资产的 SHA256 校验文件..."
rm -f "$DIST_DIR"/SHA256SUMS.txt   # 旧格式的合并清单不再产出
(
    cd "$DIST_DIR"
    if command -v sha256sum >/dev/null 2>&1; then
        for f in *; do
            sha256sum "$f" > "$f.sha256"
        done
    elif command -v shasum >/dev/null 2>&1; then
        for f in *; do
            shasum -a 256 "$f" > "$f.sha256"
        done
    fi
)

echo "==> 构建完成！Release 资产清单:"
ls -lh "$DIST_DIR"
