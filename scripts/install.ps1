# Argo 一键安装脚本（Windows PowerShell 版）
# 与 install.sh 对齐：克隆/更新真源 + 依赖 + 可选 Skill 链接
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/install.ps1
#   $env:ARGO_HOME = "C:\path\to\argo"; powershell -ExecutionPolicy Bypass -File scripts/install.ps1
#
# 环境变量：
#   ARGO_HOME         安装目录，默认 $env:USERPROFILE\.local\share\argo
#   ARGO_REPO         仓库地址，默认 https://github.com/taxueseek/argo.git
#   ARGO_BRANCH       分支，默认 main
#   ARGO_SKIP_PIP     设为 1 则跳过 pip 安装
#   ARGO_LINK_TARGETS 分号分隔的 Skill 入口路径（可选）

$ErrorActionPreference = "Stop"

$Repo = if ($env:ARGO_REPO) { $env:ARGO_REPO } else { "https://github.com/taxueseek/argo.git" }
$Branch = if ($env:ARGO_BRANCH) { $env:ARGO_BRANCH } else { "main" }
$InstallDir = if ($env:ARGO_HOME) { $env:ARGO_HOME } else { Join-Path $env:USERPROFILE ".local\share\argo" }
$SkipPip = ($env:ARGO_SKIP_PIP -eq "1")
$LinkTargets = @()
foreach ($a in $args) {
    if ($a -eq "--link" -or $a -eq "--to") { continue }
    if ($args -contains $a) { continue }  # --to 的值在下一轮处理
    $LinkTargets += $a
}
# 简单解析 --to <path>
for ($i = 0; $i -lt $args.Count; $i++) {
    if ($args[$i] -eq "--to") { $LinkTargets += $args[$i + 1] }
}
if ($env:ARGO_LINK_TARGETS) {
    $LinkTargets += ($env:ARGO_LINK_TARGETS -split ";")
}

Write-Host "==> Argo 安装目录: $InstallDir"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "需要 git，请先安装后再试。"
    exit 1
}
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "需要 Python 3.10+，请先安装后再试。"
    exit 1
}

$pyOk = python -c "import sys; print(1 if sys.version_info >= (3, 10) else 0)"
if ($pyOk.Trim() -ne "1") {
    Write-Error "当前 Python 版本低于 3.10。"
    exit 1
}

if (Test-Path (Join-Path $InstallDir ".git")) {
    Write-Host "==> 已有仓库，拉取更新 ($Branch)…"
    git -C $InstallDir fetch --depth 1 origin $Branch
    git -C $InstallDir checkout $Branch
    git -C $InstallDir pull --ff-only origin $Branch
    if ($LASTEXITCODE -ne 0) { Write-Host "[warn] pull --ff-only 失败，跳过" }
} elseif ((Test-Path $InstallDir) -and (Test-Path (Join-Path $InstallDir "scripts\search.py"))) {
    Write-Host "==> 目录已存在且含 Argo 源码，跳过克隆: $InstallDir"
} else {
    Write-Host "==> 克隆仓库…"
    New-Item -ItemType Directory -Force -Path (Split-Path $InstallDir) | Out-Null
    git clone --depth 1 --branch $Branch $Repo $InstallDir
}

if (-not $SkipPip) {
    Write-Host "==> 安装依赖 (PyYAML)…"
    python -m pip install pyyaml
    if ($LASTEXITCODE -ne 0) { Write-Host "[warn] pip 安装 PyYAML 失败，可手动: pip install pyyaml" }
    Write-Host "==> 安装可选增强 (curl_cffi: TLS 指纹伪造，缺失不影响核心功能)…"
    python -m pip install curl_cffi
    if ($LASTEXITCODE -ne 0) { Write-Host "[warn] pip 安装 curl_cffi 失败（可选依赖）" }
}

if ($LinkTargets.Count -gt 0 -or (Test-Path (Join-Path $InstallDir "installs.local.yaml"))) {
    Write-Host "==> 链接 Skill 入口（符号链接回真源，不复制）…"
    $linkArgs = @()
    foreach ($t in $LinkTargets) { if ($t) { $linkArgs += @("--to", $t) } }
    python (Join-Path $InstallDir "scripts\link_source.py") @linkArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[warn] 链接未完成。可稍后手动:"
        Write-Host "  python $InstallDir\scripts\link_source.py --to C:\Users\you\.claude\skills\argo"
    }
}

Write-Host ""
Write-Host "==> 安装完成"
Write-Host ""
Write-Host "快速验证:"
Write-Host "  python $InstallDir\scripts\search.py ""Python asyncio"" --json"
Write-Host "  python $InstallDir\scripts\search.py --list-engines"
Write-Host ""
Write-Host "启动 MCP（给 Claude / Kimi / Cursor 等用）:"
Write-Host "  python $InstallDir\scripts\mcp_server.py"
Write-Host ""
Write-Host "或用 npx（需 Node.js 18+）:"
Write-Host "  npx -y argo-search"
Write-Host ""
Write-Host "客户端 MCP 配置示例（路径请按本机替换）:"
Write-Host @"
{
  "mcpServers": {
    "argo": {
      "command": "python",
      "args": ["$InstallDir\scripts\mcp_server.py"]
    }
  }
}
"@
Write-Host ""
Write-Host "可选：把 Skill 挂到本机 Agent 目录（不复制代码）:"
Write-Host "  python $InstallDir\scripts\link_source.py --to $env:USERPROFILE\.claude\skills\argo"
Write-Host "  python $InstallDir\scripts\link_source.py --to $env:USERPROFILE\.agents\skills\argo"
Write-Host ""
Write-Host "文档: https://github.com/taxueseek/argo"
