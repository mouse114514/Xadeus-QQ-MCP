"""Entry point that hardcodes --qq 3838379219 to work around opencode config caching."""
import sys
sys.argv.extend(["--qq", "3838379219"])
from qq_agent_mcp.__main__ import main
main()
