&lt;#
.SYNOPSIS
    Restart the Xadeus-QQ-MCP service.
.DESCRIPTION
    Kills all stale MCP python processes, then waits for opencode
    (or your AI agent) to auto-restart the MCP subprocess.
    If auto-restart fails, prompts you to restart the agent manually.
#&gt;

$VENV_PY = Join-Path $PSScriptRoot ".venv" "Scripts" "python.exe"
$PROJECT = Split-Path $PSScriptRoot -Leaf

Write-Host "=== Xadeus-QQ-MCP Restart Helper ===" -ForegroundColor Cyan

# ── 1. Kill all MCP processes ──
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' AND CommandLine LIKE '%qq_agent_mcp%'"
if ($procs) {
    Write-Host "Killing $($procs.Count) stale MCP process(es)..." -ForegroundColor Yellow
    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep 2
    Write-Host "Done." -ForegroundColor Green
} else {
    Write-Host "No stale MCP processes found." -ForegroundColor Green
}

# ── 2. Wait for auto-restart ──
Write-Host "Waiting for opencode to auto-restart the MCP subprocess..." -ForegroundColor Cyan
$found = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep 2
    $newProcs = Get-CimInstance Win32_Process -Filter "Name='python.exe' AND CommandLine LIKE '%qq_agent_mcp%'"
    if ($newProcs) {
        Write-Host "MCP restarted! PID(s): $($newProcs.ProcessId -join ', ')" -ForegroundColor Green
        $found = $true
        break
    }
}

if (-not $found) {
    Write-Host ""
    Write-Host "WARNING: opencode did not auto-restart the MCP subprocess." -ForegroundColor Red
    Write-Host "This is normal after multiple restarts (opencode uses restart backoff)." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "To fix: restart your AI agent (opencode / Cursor / Claude Desktop)." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  If you are using opencode CLI:" -ForegroundColor Cyan
    Write-Host "  1. Restart opencode (the MCP will start automatically with the new session)" -ForegroundColor White
    Write-Host "  2. The QQ_OVERRIDE env var avoids needing to edit --qq in config" -ForegroundColor White
    Write-Host ""
    Write-Host "  Environment variable (optional, set before starting agent):" -ForegroundColor Cyan
    Write-Host "    `$env:QQ_OVERRIDE = `"YOUR_QQ_NUMBER`"" -ForegroundColor White
}
