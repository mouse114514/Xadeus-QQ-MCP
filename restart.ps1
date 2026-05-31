# Xadeus-QQ-MCP Restart Helper
# Kills stale MCP processes, waits for auto-restart.
# If auto-restart fails, prompts manual restart.

$VENV_PY = Join-Path -Path $PSScriptRoot -ChildPath ".venv\Scripts\python.exe"
$PROJECT = Split-Path -Path $PSScriptRoot -Leaf

Write-Host "=== Xadeus-QQ-MCP Restart Helper ===" -ForegroundColor Cyan

# Step 1: Kill all stale MCP processes
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' AND CommandLine LIKE '%qq_agent_mcp%'"
if ($procs) {
    Write-Host "Killing $($procs.Count) stale MCP process(es)..." -ForegroundColor Yellow
    $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep 2
    Write-Host "Done." -ForegroundColor Green
} else {
    Write-Host "No stale MCP processes found." -ForegroundColor Green
}

# Step 2: Wait for opencode to auto-restart
Write-Host "Waiting for opencode to auto-restart MCP..." -ForegroundColor Cyan
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
    Write-Host "Solution: restart your AI agent (opencode / Cursor / Claude Desktop)." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  If using opencode CLI:" -ForegroundColor Cyan
    Write-Host "  1. Restart opencode (MCP will auto-start)" -ForegroundColor White
    Write-Host "  2. Set QQ_OVERRIDE env var before starting:" -ForegroundColor White
    Write-Host "     `$env:QQ_OVERRIDE = `"YOUR_QQ_NUMBER`"" -ForegroundColor White
}
