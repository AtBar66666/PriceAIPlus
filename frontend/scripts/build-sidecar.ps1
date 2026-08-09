param(
    [string]$TargetTriple = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$crawlerDir = Join-Path $repoRoot "crawler"
$python = Join-Path $crawlerDir ".venv\Scripts\python.exe"
$entryPoint = Join-Path $crawlerDir "backend_entry.py"
$binariesDir = Join-Path $repoRoot "frontend\src-tauri\binaries"
$workDir = Join-Path $crawlerDir "build\pyinstaller"
$pickaiSnapshot = Join-Path $crawlerDir "data\pickai_snapshot.json"

if (-not (Test-Path $python)) {
    throw "未找到后端虚拟环境：$python"
}

if (-not $TargetTriple) {
    $TargetTriple = (& rustc --print host-tuple).Trim()
}

if (-not $TargetTriple) {
    throw "无法读取 Rust target triple"
}

New-Item -ItemType Directory -Force -Path $binariesDir | Out-Null
New-Item -ItemType Directory -Force -Path $workDir | Out-Null

$outputName = "bipai-backend-$TargetTriple"

$seedArgs = @()
if (Test-Path $pickaiSnapshot) {
    $seedArgs = @("--add-data", "$pickaiSnapshot;seed")
    Write-Host "Bundling PickAI seed: $pickaiSnapshot"
} else {
    Write-Warning "PickAI snapshot seed not found; first launch will build it online."
}

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --noconsole `
    --name $outputName `
    --distpath $binariesDir `
    --workpath $workDir `
    --specpath $workDir `
    --paths $crawlerDir `
    --collect-submodules app `
    --collect-submodules uvicorn `
    --collect-all curl_cffi `
    @seedArgs `
    $entryPoint

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 构建失败，退出码：$LASTEXITCODE"
}

$sidecar = Join-Path $binariesDir "$outputName.exe"
if (-not (Test-Path $sidecar)) {
    throw "构建完成但未找到 sidecar：$sidecar"
}

Write-Host "Sidecar ready: $sidecar"
