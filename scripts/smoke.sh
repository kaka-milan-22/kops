#!/usr/bin/env bash
# Smoke test: start the kops MCP server, run the initialize -> tools/list
# handshake, and print the tool names it serves. No cluster needed.
set -euo pipefail
{
  printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
  printf '%s\n' '{"jsonrpc":"2.0","method":"notifications/initialized"}'
  printf '%s\n' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
} | uv run kops 2>/dev/null \
  | jq -r 'select(.id==2) | "✓ kops serves \(.result.tools|length) tools:", (.result.tools[] | "  • \(.name)")'
