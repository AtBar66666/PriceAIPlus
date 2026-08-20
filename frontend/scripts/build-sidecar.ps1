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
$distDir = Join-Path $workDir "dist"
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

# onedir 而非 onefile：onefile 每次启动都要把全部内容自解压到临时目录，
# 冷启动动辄数秒。onedir 由 Rust 侧按版本解压一次后直接运行，启动快得多。
& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --noconsole `
    --name $outputName `
    --distpath $distDir `
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

$appDir = Join-Path $distDir $outputName
$sidecarExe = Join-Path $appDir "$outputName.exe"
if (-not (Test-Path $sidecarExe)) {
    throw "构建完成但未找到 sidecar：$sidecarExe"
}

# 打成 zip 嵌进 Tauri 主程序；zip 根目录直接是 exe 与 _internal。
$zipPath = Join-Path $binariesDir "$outputName.zip"
if (Test-Path $zipPath) {
    Remove-Item -Force $zipPath
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $appDir,
    $zipPath,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $false
)

# 旧的 onefile 产物不再使用，删掉避免被误嵌。
$legacyExe = Join-Path $binariesDir "$outputName.exe"
if (Test-Path $legacyExe) {
    Remove-Item -Force $legacyExe
}

$zipItem = Get-Item $zipPath
Write-Host ("Sidecar ready: {0} ({1:N2} MB)" -f $zipItem.FullName, ($zipItem.Length / 1MB))
