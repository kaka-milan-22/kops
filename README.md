# kops

Read-only `kubectl` helper exposed to Claude Code via MCP. Five tools:

| Tool | What it does |
|------|------|
| `k8s_get` | List/fetch resources, returns summarized key fields |
| `k8s_describe` | `kubectl describe` text output for one resource |
| `k8s_logs` | Pod logs with tail / since / previous flags |
| `k8s_events` | Recent events filtered by namespace / kind / name |
| `k8s_triage` | ⭐ One-shot cluster health scan — start here for diagnostics |

All tools are strictly read-only. Verbs (`get`, `describe`, `logs`) are hardcoded; user input only fills argument values.

## Install

```bash
cd /Users/kaka/claude/kops
uv sync
```

## Smoke test (no cluster needed)

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | uv run kops
```

Expect a JSON response listing the 5 tools with input schemas.

## Visual debug with MCP Inspector

```bash
npx @modelcontextprotocol/inspector uv --directory /Users/kaka/claude/kops run kops
```

Open the URL it prints, click each tool, exercise the parameters.

## Register with Claude Code

Add to `~/.claude/settings.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "kops": {
      "command": "uv",
      "args": ["--directory", "/Users/kaka/claude/kops", "run", "kops"]
    }
  }
}
```

Reload Claude Code (or open a new session). Tools appear as
`mcp__kops__k8s_get`, `mcp__kops__k8s_triage`, etc.

### Multi-cluster (kubeconfig isolation)

To talk to a foreign cluster without polluting `~/.kube/config`, register a
separate server entry with its own `KUBECONFIG`:

```json
{
  "mcpServers": {
    "kops-qa": {
      "command": "uv",
      "args": ["--directory", "/Users/kaka/claude/kops", "run", "kops"],
      "env": { "KUBECONFIG": "/path/to/qa-cluster.yaml" }
    }
  }
}
```

Tools then surface as `mcp__kops_qa__k8s_triage` etc, fully isolated.

## End-to-end smoke (with a kind cluster)

```bash
kind create cluster --name kops-test
kubectl run broken --image=nonexistent:fake --restart=Never
sleep 30
```

Then in Claude Code, ask: "this cluster has problems, what's wrong?"

Expected: Claude calls `mcp__kops__k8s_triage` first, sees the
`broken` pod in `ImagePullBackOff`, then `k8s_describe` for root cause.

## Safety

- Verb hardcoded in each tool function — no input path can switch to `delete` / `apply` / `patch`
- All names/namespaces validated against `^[a-zA-Z0-9._-]{1,253}$`
- `subprocess.run([...], shell=False)` — no shell metacharacters reach a shell
- `kubectl` invoked with 30s timeout; `tail` clamped to 1000 lines
- Output size capped (30KB describe, 50KB logs)

## Extending

Add another tool by writing a new `@mcp.tool()` function in `src/kops/server.py`.
The verb stays hardcoded; only argument values come from the input. Type hints
become the JSON-RPC input schema automatically (FastMCP does this).
