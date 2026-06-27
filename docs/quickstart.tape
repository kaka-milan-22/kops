# Render with:  vhs docs/quickstart.tape   (run from the repo root)
Output docs/quickstart.gif

Set Shell "bash"
Set FontSize 20
Set Width 1180
Set Height 560
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms
Set Padding 26
Set BorderRadius 8

# --- hidden setup: ensure tools on PATH + clean screen ---
Hide
Type 'export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"'
Enter
Type 'cd ~/claude/kops'
Enter
Type 'clear'
Enter
Show

# --- visible quickstart ---
Type "# kops — read-only kubectl, exposed as an MCP server"
Enter
Sleep 900ms
Type "uv sync          # install deps (one-time)"
Enter
Sleep 4s
Type "# register it in Claude Code:"
Enter
Type "#   claude mcp add kops -- uv run kops"
Enter
Sleep 900ms
Type "# verify it serves the 6 read-only tools (no cluster needed):"
Enter
Type "./scripts/smoke.sh"
Enter
Sleep 4500ms
