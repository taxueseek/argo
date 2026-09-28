# Argo 一键安装脚本（Windows PowerShell 版）
# 与 install.sh 对齐：克隆/更新真源 + 依赖 + 可选 Skill 链接
# 用法：
#   powershell -ExecutionPolicy RemoteSigned -File scripts/install.ps1
#   powershell -ExecutionPolicy RemoteSigned -File scripts/install.ps1 --link C:\path\to\skill
#   $env:ARGO_HOME = "C:\path\to\argo"; powershell -ExecutionPolicy RemoteSigned -File scripts/install.ps1
#
# 安全提示：
#   - 本脚本经 git clone 落地（非网络下载），RemoteSigned 已足够放行本地脚本。
#     若因下载文件的 MOTW 被拦，先 `Unblock-File scripts\install.ps1` 再运行，
#     避免使用 `-ExecutionPolicy Bypass`（微软安全基线明确反对完全绕过执行策略）。
#   - 供应链加固：设 ARGO_PIN=<commit SHA> 使克隆后校验 HEAD 一致（与 install.sh 对齐）。
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
$Pin = if ($env:ARGO_PIN) { $env:ARGO_PIN } else { "" }
$LinkTargets = @()
# 逐个参数解析：--link <path> / --to <path> 收集其值，其他未知参数跳过
for ($i = 0; $i -lt $args.Count; $i++) {
    $a = $args[$i]
    if ($a -eq "--link" -or $a -eq "--to") {
        if ($i + 1 -lt $args.Count) { $LinkTargets += $args[$i + 1]; $i++ }
        continue
    }
    # 裸路径参数（未带开关）视为链接目标
    $LinkTargets += $a
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
    Write-Error "需要 Python 3.9+，请先安装后再试。"
    exit 1
}

# 版本下限与 bin/argo 的 MIN_PYTHON、install.sh 保持一致：改一处就要改另外两处。
# 探测 python；无别名时回退 Windows 官方 py 启动器（bin/argo 候选链同样支持）
$script:py = @("python")
$pyOk = (python -c "import sys; print(1 if sys.version_info >= (3, 9) else 0)" 2>$null).Trim()
if ($pyOk -ne "1") {
    $py3Ok = (py -3 -c "import sys; print(1 if sys.version_info >= (3, 9) else 0)" 2>$null).Trim()
    if ($py3Ok -eq "1") {
        $script:py = @("py", "-3")
    } else {
        Write-Error "当前 Python 版本低于 3.9，或 python/py 启动器不在 PATH。"
        exit 1
    }
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

# 供应链加固：固定 commit 校验（ARGO_PIN），与 install.sh 对齐
if ($Pin) {
    $actualHead = if (Test-Path (Join-Path $InstallDir ".git")) {
        (git -C $InstallDir rev-parse HEAD 2>$null)
    } else { "unknown" }
    if ($actualHead.Trim() -ne $Pin) {
        Write-Error "供应链校验失败：固定 commit 为 $Pin，实际 HEAD 为 $actualHead。请人工核查仓库来源。"
        exit 1
    }
    Write-Host "==> 供应链校验通过: HEAD=$Pin"
}

if (-not $SkipPip) {
    Write-Host "==> 安装依赖 (PyYAML)…"
    & $script:py -m pip install pyyaml
    if ($LASTEXITCODE -ne 0) { Write-Host "[warn] pip 安装 PyYAML 失败，可手动: pip install pyyaml" }
    Write-Host "==> 安装可选增强 (curl_cffi: TLS 指纹伪造，缺失不影响核心功能)…"
    & $script:py -m pip install curl_cffi
    if ($LASTEXITCODE -ne 0) { Write-Host "[warn] pip 安装 curl_cffi 失败（可选依赖）" }
}

if ($LinkTargets.Count -gt 0 -or (Test-Path (Join-Path $InstallDir "installs.local.yaml"))) {
    Write-Host "==> 链接 Skill 入口（符号链接回真源，不复制）…"
    $linkArgs = @()
    foreach ($t in $LinkTargets) { if ($t) { $linkArgs += @("--to", $t) } }
    & $script:py (Join-Path $InstallDir "scripts\link_source.py") @linkArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[warn] 链接未完成。可稍后手动:"
        Write-Host "  python $InstallDir\scripts\link_source.py --to C:\Users\you\.claude\skills\argo"
    }
}

Write-Host ""
Write-Host "==> 安装完成"
Write-Host ""
$pyLabel = $script:py -join " "
Write-Host "快速验证:"
Write-Host "  & $pyLabel $InstallDir\scripts\search.py ""Python asyncio"" --json"
Write-Host "  & $pyLabel $InstallDir\scripts\search.py --list-engines"
Write-Host ""
Write-Host "启动 MCP（给 Claude / Kimi / Cursor 等用）:"
Write-Host "  & $pyLabel $InstallDir\scripts\mcp_server.py"
Write-Host ""
Write-Host "或用 npx（需 Node.js 18+，推荐，不依赖 npm 发版）:"
Write-Host "  npx -y github:taxueseek/argo"
Write-Host ""
Write-Host "客户端 MCP 配置示例（command 用解释器绝对路径：GUI 宿主的子进程 PATH 常缺 python）:"
$pythonExe = (Get-Command $script:py[0] -ErrorAction SilentlyContinue).Source
if (-not $pythonExe) { $pythonExe = $script:py[0] }
Write-Host @"
{
  "mcpServers": {
    "argo": {
      "command": "$pythonExe",
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
