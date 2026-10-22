#!/usr/bin/env bash
# 构建 Argo Release 资产包（tar.gz、wheel 与 sha256 校验和）
#
# 用法：
#   bash scripts/build_release_assets.sh
#   产物输出至 dist/ 目录。

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VERSION=$(python3 -c 'import json; print(json.load(open("package.json"))["version"])')
DIST_DIR="$ROOT_DIR/dist"
STAGE_DIR="$DIST_DIR/argo-$VERSION"

echo "==> 开始构建 Argo Release 资产包 (v$VERSION)..."
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR" "$STAGE_DIR"

# 1. 拷贝运行时必需核心文件至 staging 目录（排除测试/缓存/私有数据）
echo "--> 组织源码资产..."
cp -r bin config.yaml engines backends data mcp scripts sub-skills README.md SKILL.md LICENSE package.json pyproject.toml "$STAGE_DIR/"

# 清理 staging 内的多余缓存与测试
find "$STAGE_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$STAGE_DIR" -name "*.pyc" -delete 2>/dev/null || true
rm -rf "$STAGE_DIR/data/jobs" 2>/dev/null || true
find "$STAGE_DIR/sub-skills" -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true
find "$STAGE_DIR/sub-skills" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# 2. 打包 tar.gz
TARBALL="$DIST_DIR/argo-v$VERSION.tar.gz"
echo "--> 生成源码压缩包: $TARBALL"
tar -czf "$TARBALL" -C "$DIST_DIR" "argo-$VERSION"
rm -rf "$STAGE_DIR"

# 3. 如果环境已安装 build 模块，打出标准 Python wheel
if python3 -c 'import build' 2>/dev/null; then
    echo "--> 构建 Python Wheel 与 Sdist..."
    python3 -m build --outdir "$DIST_DIR"
elif python3 -c 'import setuptools, wheel' 2>/dev/null; then
    echo "--> 使用 setuptools 构建 Wheel..."
    python3 setup.py bdist_wheel --dist-dir "$DIST_DIR" 2>/dev/null || true
fi

# 4. 生成 SHA256 校验和清单
echo "--> 计算 SHA256 校验和..."
(
    cd "$DIST_DIR"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum * > SHA256SUMS.txt
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 * > SHA256SUMS.txt
    fi
)

echo "==> 构建完成！Release 资产清单:"
ls -lh "$DIST_DIR"
