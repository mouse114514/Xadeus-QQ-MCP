@echo off
chcp 65001 >nul
cd /d "C:\Users\Administrator\Desktop\work\Xadeus-QQ-MCP"
echo Starting QQ MCP Server for QQ 1745557997...
echo Make sure NapCatQQ is running first!
echo.
.venv\Scripts\python -m qq_agent_mcp --qq 1745557997
pause
