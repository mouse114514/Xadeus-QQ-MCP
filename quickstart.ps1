param(
    [string]$qq,
    [string]$configFile,
    [string]$napcatDir,
    [int]$httpPort = 3000,
    [int]$wsPort = 3001,
    [switch]$help
)

if ($help) {
    @"
Xadeus-QQ-MCP Quick Start

Usage:
    .\quickstart.ps1 -qq 123456
    .\quickstart.ps1 -configFile config.json
    .\quickstart.ps1 -qq 123456 -napcatDir "C:\NapCatQQ"

Config file format (config.json):
    { "qq": "123456", "napcat_path": "C:\\NapCatQQ", "http_port": 3000, "ws_port": 3001 }
"@
    exit
}

# ── Load config file if provided ──
if ($configFile) {
    $cfg = Get-Content $configFile | ConvertFrom-Json
    if (-not $qq) { $qq = $cfg.qq }
    if (-not $napcatDir) { $napcatDir = $cfg.napcat_path }
    if ($cfg.http_port) { $httpPort = $cfg.http_port }
    if ($cfg.ws_port) { $wsPort = $cfg.ws_port }
}

if (-not $qq) {
    Write-Host "ERROR: -qq is required. Use -qq YOUR_QQ or -configFile path" -ForegroundColor Red
    exit 1
}

$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$VENV_PY = Join-Path -Path $ROOT -ChildPath ".venv\Scripts\python.exe"

Write-Host "=== Xadeus-QQ-MCP Quick Start ===" -ForegroundColor Cyan
Write-Host "QQ: $qq"

# ── Step 1: Find NapCat ──
if (-not $napcatDir) {
    $candidates = @(
        "$env:USERPROFILE\Desktop\work\napcat-qq",
        "$env:USERPROFILE\Desktop\napcat-qq",
        "C:\NapCatQQ",
        "C:\Program Files\NapCatQQ"
    )
    foreach ($c in $candidates) {
        if (Test-Path "$c\NapCatWinBootMain.exe") {
            $napcatDir = $c
            break
        }
    }
}
if (-not $napcatDir) {
    Write-Host "ERROR: Cannot find NapCat. Pass -napcatDir explicitly." -ForegroundColor Red
    exit 1
}
Write-Host "NapCat: $napcatDir" -ForegroundColor Green

# ── Step 2: Find NapCat version dir ──
$versions = Join-Path -Path $napcatDir -ChildPath "versions"
$versionDir = $null
if (Test-Path $versions) {
    $dirs = Get-ChildItem $versions -Directory | Sort-Object Name -Descending
    foreach ($d in $dirs) {
        $napcatMjs = "$($d.FullName)\resources\app\napcat\napcat.mjs"
        if (Test-Path $napcatMjs) {
            $versionDir = $d.Name
            break
        }
    }
}
if (-not $versionDir) {
    Write-Host "ERROR: Cannot find NapCat version directory." -ForegroundColor Red
    exit 1
}
Write-Host "Version: $versionDir" -ForegroundColor Green

# ── Step 3: Write NapCat onebot11 config ──
$napcatConfigDir = Join-Path -Path $napcatDir -ChildPath "versions\$versionDir\resources\app\napcat\config"
if (-not (Test-Path $napcatConfigDir)) {
    New-Item -ItemType Directory -Path $napcatConfigDir -Force | Out-Null
}
$onebotConfig = @{
    network = @{
        httpServers = @(@{
            name = "http"
            enable = $true
            port = $httpPort
            host = "0.0.0.0"
        })
        websocketServers = @(@{
            name = "ws"
            enable = $true
            port = $wsPort
            host = "0.0.0.0"
        })
        httpClients = @()
        httpSseServers = @()
        websocketClients = @()
        plugins = @()
    }
    musicSignUrl = ""
    enableLocalFile2Url = $false
    parseMultMsg = $false
}
$onebotFile = Join-Path -Path $napcatConfigDir -ChildPath "onebot11_$qq.json"
$onebotConfig | ConvertTo-Json -Depth 10 | Set-Content $onebotFile -Encoding UTF8
Write-Host "NapCat config written: $onebotFile" -ForegroundColor Green

# ── Step 4: Set up Python venv ──
if (-not (Test-Path $VENV_PY)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    if (Get-Command "uv" -ErrorAction SilentlyContinue) {
        uv venv "$ROOT\.venv"
        uv sync --directory $ROOT
    } elseif (Get-Command "python" -ErrorAction SilentlyContinue) {
        python -m venv "$ROOT\.venv"
        & $VENV_PY -m pip install -e "$ROOT"
    } else {
        Write-Host "ERROR: Neither uv nor python found. Install Python first." -ForegroundColor Red
        exit 1
    }
    Write-Host "Virtual environment ready." -ForegroundColor Green
} else {
    Write-Host "Virtual environment exists." -ForegroundColor Green
}

# ── Step 5: Set QQ_OVERRIDE ──
$mainPy = Join-Path -Path $ROOT -ChildPath "src\qq_agent_mcp\__main__.py"
$content = Get-Content $mainPy -Raw
if ($content -match "QQ_OVERRIDE = os\.environ\.get\(""QQ_OVERRIDE"") or ""([^""]*)""") {
    $oldQQ = $matches[1]
    if ($oldQQ -ne $qq) {
        $newContent = $content -replace '(QQ_OVERRIDE = os\.environ\.get\("QQ_OVERRIDE"\) or ")[^"]*(")', "`${1}$qq`$2"
        Set-Content $mainPy -Value $newContent -Encoding UTF8
        Write-Host "QQ_OVERRIDE updated to $qq in __main__.py" -ForegroundColor Green
    } else {
        Write-Host "QQ_OVERRIDE already set to $qq" -ForegroundColor Green
    }
}

# ── Step 6: Write batch to start NapCat ──
$napcatInternal = Join-Path -Path $napcatDir -ChildPath "versions\$versionDir\resources\app\napcat"
$loadJs = Join-Path -Path $napcatInternal -ChildPath "loadNapCat.js"
$napcatMjs = "file:///$($napcatInternal.Replace('\', '/'))/napcat.mjs"
$batContent = @"
@echo off
chcp 65001 >nul
title NapCatQQ
cd /d "$napcatDir"
> "$loadJs" echo (async () => {await import("$napcatMjs".replace(/\\/g,"/"))})()
start "NapCatQQ" "$napcatDir\NapCatWinBootMain.exe" "QQ.exe" -q $qq
exit
"@
$batFile = Join-Path -Path $ROOT -ChildPath "start_napcat.bat"
Set-Content $batFile -Value $batContent -Encoding Default
Write-Host "NapCat start script: $batFile" -ForegroundColor Green

# ── Step 7: Print instructions ──
Write-Host ""
Write-Host "=== Setup Complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "1. Start NapCat: .\start_napcat.bat" -ForegroundColor White
Write-Host "2. Login QQ via QR code" -ForegroundColor White
Write-Host "3. Add this to your agent's MCP config:" -ForegroundColor White
Write-Host ""
Write-Host '   opencode (~/.config/opencode/opencode.json):' -ForegroundColor Cyan
Write-Host "   {"
Write-Host '     "mcp": {'
Write-Host '       "qq-agent": {'
Write-Host '         "type": "local",'
Write-Host '         "command": ['
Write-Host "           `"$VENV_PY`","
Write-Host '           "-m", "qq_agent_mcp",'
Write-Host '           "--qq", "'$qq'"'
Write-Host '         ],'
Write-Host '         "enabled": true,'
Write-Host '         "timeout": 120000'
Write-Host "       }"
Write-Host "     }"
Write-Host "   }"
Write-Host ""
Write-Host "4. Restart your AI agent" -ForegroundColor White
Write-Host "5. Send a QQ message to test wake" -ForegroundColor White
Write-Host ""
Write-Host "If you change your QQ number later, just re-run:" -ForegroundColor Yellow
Write-Host "  .\quickstart.ps1 -qq NEW_QQ" -ForegroundColor White
