$ErrorActionPreference = "Stop"

$frontendDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$releaseDir = Join-Path $frontendDir "src-tauri\target\release"
$source = Join-Path $releaseDir "app.exe"
$portableDir = Join-Path $releaseDir "portable"
$icon = Join-Path $frontendDir "src-tauri\icons\icon.ico"
# Windows PowerShell 5.1 treats UTF-8 scripts without a BOM as ANSI.
# Build the Chinese filename from Unicode code points to prevent mojibake.
$portableName = "$([char]0x6BD4)$([char]0x724C).exe"
$destination = Join-Path $portableDir $portableName

if (-not (Test-Path $source)) {
    throw "Tauri executable not found: $source"
}
if (-not (Test-Path $icon)) {
    throw "Windows icon not found: $icon"
}

# A running portable build locks its own executable and keeps the old backend
# process alive. Copies live both in the release folder and on the Desktop, so
# stop whichever instance is running before replacing either file.
$desktopDir = [Environment]::GetFolderPath("Desktop")
$desktopDestination = if ($desktopDir) { Join-Path $desktopDir $portableName } else { $null }

function Stop-RunningInstance([string] $path) {
    if (-not $path) { return }
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.ExecutablePath -and
            [string]::Equals($_.ExecutablePath, $path, [System.StringComparison]::OrdinalIgnoreCase)
        } |
        ForEach-Object {
            & "$env:SystemRoot\System32\taskkill.exe" /PID $_.ProcessId /T /F | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to stop running portable executable (PID $($_.ProcessId))"
            }
        }
}

Stop-RunningInstance $destination
Stop-RunningInstance $desktopDestination

# Tauri embeds icon.ico during compilation. Rewriting that resource afterwards
# with rcedit can corrupt the executable and make WebView exit on a white screen.

New-Item -ItemType Directory -Force -Path $portableDir | Out-Null
Get-ChildItem -Path $portableDir -Filter "*.exe" -File |
    Where-Object { $_.Name -ne $portableName } |
    Remove-Item -Force
Copy-Item -Force -Path $source -Destination $destination

$item = Get-Item $destination
Write-Host ("Portable ready: {0} ({1:N2} MB)" -f $item.FullName, ($item.Length / 1MB))

# Always drop a fresh copy on the Desktop so the latest portable build is one
# double-click away. The backend runs from %LOCALAPPDATA%, so location is free.
if ($desktopDestination) {
    # Give the just-killed Desktop instance a moment to release its file lock.
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        try {
            Copy-Item -Force -Path $destination -Destination $desktopDestination
            break
        } catch {
            if ($attempt -eq 9) { throw }
            Start-Sleep -Milliseconds 300
        }
    }
    $desktopItem = Get-Item $desktopDestination
    Write-Host ("Desktop copy: {0} ({1:N2} MB)" -f $desktopItem.FullName, ($desktopItem.Length / 1MB))
} else {
    Write-Warning "Desktop folder not found; skipped Desktop copy."
}
