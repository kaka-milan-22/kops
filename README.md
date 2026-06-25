# kops

Read-only `kubectl` helper exposed to Claude Code via MCP. Six tools, all strictly read-only — verbs (`get`, `describe`, `logs`) are hardcoded; user input only fills argument values, never the verb itself.

| Tool | What it does |
|------|------|
| `k8s_get` | List/fetch resources, returns summarized key fields per kind |
| `k8s_describe` | `kubectl describe` text output for one resource |
| `k8s_logs` | Pod logs with `tail` / `since` / `previous` flags |
| `k8s_events` | Recent events filtered by namespace / kind / name |
| `k8s_triage` | ⭐ One-shot cluster health scan — start here for diagnostics |
| `k8s_inventory` | ⭐ One-shot comprehensive snapshot — start here for documentation / audits |

## Why

`kubectl` over Bash gives Claude text tables that need re-parsing every turn. Wrapping it as MCP returns **structured JSON** Claude can reason over directly — fewer tokens, fewer parse errors, and built-in safety boundaries (read-only verbs, name validation, output size caps).

Two aggregator tools (`k8s_triage`, `k8s_inventory`) compress common multi-step queries into single round-trips:

- `k8s_triage` — "what's broken?" → 4 concurrent kubectl calls, returns problem pods + warning events + unhealthy nodes + stale deployments
- `k8s_inventory` — "show me everything" → 14+ concurrent kubectl calls, returns cluster-wide snapshot grouped by namespace. Replaces ~50 individual `k8s_get` calls (~6× faster end-to-end for cluster docs).

## Install

Requires [`uv`](https://docs.astral.sh/uv/) and `kubectl` in your `PATH`.

```bash
git clone https://github.com/kaka-milan-22/kops.git
cd kops
uv sync
```

> The commands below use `/path/to/kops` for the absolute path of this clone —
> replace it with your actual path (e.g. the output of `pwd` run from inside the
> cloned directory). `uv --directory` needs an absolute path.

## Smoke test (no cluster needed)

MCP requires a handshake (`initialize` → `notifications/initialized`) before any business request, so a bare `tools/list` over stdin is rejected with `Received request before initialization was complete`. Feed all three messages in order:

```bash
{
  printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
  printf '%s\n' '{"jsonrpc":"2.0","method":"notifications/initialized"}'
  printf '%s\n' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
} | uv run kops
```

Expect: an `initialize` response, then a `tools/list` response listing the **6 tools** with input schemas. (The notification has no `id` and produces no reply.)

## Visual debug with MCP Inspector

For interactive debugging, skip the raw stdio dance and use the official tools — they handle the handshake for you:

```bash
# Option A: MCP Inspector (browser UI)
npx @modelcontextprotocol/inspector uv --directory /path/to/kops run kops

# Option B: mcp dev (bundled with the mcp[cli] extra already in deps)
uv run mcp dev src/kops/server.py
```

Open the URL each prints, click a tool, exercise its parameters.

## Register with Claude Code

```bash
claude mcp add -s user kops -- uv --directory /path/to/kops run kops
```

Or manually in `~/.claude.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "kops": {
      "command": "uv",
      "args": ["--directory", "/path/to/kops", "run", "kops"]
    }
  }
}
```

Reload Claude Code (or open a new session). Tools surface as `mcp__kops__k8s_get`, `mcp__kops__k8s_triage`, `mcp__kops__k8s_inventory`, etc.

### Multi-cluster (kubeconfig isolation)

To talk to a foreign cluster without polluting `~/.kube/config`, register a separate server entry with its own `KUBECONFIG`:

```bash
claude mcp add -s user -e KUBECONFIG=/path/to/qa-cluster.yaml \
  kops-qa -- uv --directory /path/to/kops run kops
```

Tools then surface as `mcp__kops_qa__k8s_triage` etc, fully isolated.

## End-to-end smoke (with a kind cluster)

```bash
kind create cluster --name kops-test
kubectl run broken --image=nonexistent:fake --restart=Never
sleep 30
```

Then in Claude Code, ask: "this cluster has problems, what's wrong?"

Expected: Claude calls `mcp__kops__k8s_triage` first, sees the `broken` pod in `ImagePullBackOff`, then `k8s_describe` for root cause.

For a documentation example, ask: "give me a full report of this cluster".

Expected: Claude calls `mcp__kops__k8s_inventory` once and assembles a structured markdown report covering nodes, namespaces, workloads, services, exposure surface, and configuration counts.

## What `k8s_get` returns per kind

`_summarize_resource` extracts only the fields most useful for diagnostics and documentation. Avoids dumping full `spec` to keep token usage sane.

| Kind | Summarized fields |
|------|------|
| Pod | `phase`, `ready`, `restarts`, `node`, `podIP`, `images`, `containerCount`, `resources` (when declared), `reason` (when stuck) |
| Service | `type`, `clusterIP`, `externalIPs`, `loadBalancer`, `ports[]` (incl. `nodePort`) |
| Deployment | `desired`, `available`, `updated`, `ready`, `images`, `containerCount`, `resources` |
| StatefulSet / DaemonSet / ReplicaSet | `desired`, `ready`, `images`, `containerCount`, `resources` |
| Node | `ready`, `kubeletVersion`, `internalIP`, `pressures` (only if any are True) |
| Ingress | `hosts[]` |
| Namespace / generic | `name`, `namespace`, `kind`, `age`, `labels` (top 5) |

Where `resources` is the **sum across all main containers** of `requests` and `limits` (init containers excluded — they don't run concurrently with steady state, so don't add to scheduling footprint). CPU normalized to millicores, memory normalized to binary units (`Ki/Mi/Gi`). Init containers still appear in `images[]` with an `init: True` flag.

## What `k8s_inventory` returns

```python
{
  "summary": {
    "scope": "cluster-wide" | "namespace=<name>",
    "namespaces": int, "nodes": int, "pods_total": int,
    "by_kind_counts": {"deployments": N, "services": N, ...},
    "istio_present": bool,
    "include_istio": bool,
  },
  "nodes": [<summarized node>, ...],         # cluster-scoped only
  "namespaces": [
    {
      "name": "...", "age": "...", "labels": {...},
      "pods": int,                            # count only (full pod list not included)
      "deployments": [<summarized>, ...],
      "statefulsets": [...], "daemonsets": [...],
      "services": [...], "ingresses": [...], "hpa": [...],
      "pvcs": [...], "configmaps": [...], "secrets": [...],
      "jobs": [...], "cronjobs": [...],
      "istio_gateways": [...], "istio_virtualservices": [...], "istio_destinationrules": [...],
    },
    ...
  ]
}
```

ConfigMap data and Secret values are **never** returned — only metadata (names, key lists, age). This is a hard safety boundary; if you need actual config content, go through Bash + `kubectl` under explicit permission.

Pods are not included as a list (potentially huge). Use `k8s_triage` for pod health, `k8s_get pod` for specific pods.

## Safety

- **Mutation defense**: verb is hardcoded inside each tool function. User input only fills argument values, never the verb. There is no path to `delete` / `apply` / `patch` / `scale` / `exec` from any input.
- **Injection defense**: `subprocess.run([...], shell=False)` everywhere. Names/namespaces/containers validated against `^[a-zA-Z0-9._-]{1,253}$`. Selectors validated against a K8s label-selector character set.
- **Resource limits**: `kubectl` invoked with 30s timeout. Log `tail` clamped to 1000 lines. Output size capped (30KB describe, 50KB logs).
- **Context isolation**: default uses `kubectl config current-context`. The optional `context` argument can override but cannot inject a `KUBECONFIG` path. For full isolation across clusters, register a separate MCP server entry with its own `KUBECONFIG` env var.

## Extending

Add another tool by writing a new `@mcp.tool()` function in `src/kops/server.py`:

- Hardcode the verb.
- Validate inputs with the existing helpers (`_validate_kind`, `_validate_name`, `_validate_selector`, `_validate_since`).
- Call kubectl via `_run_kubectl([...])`.
- Reuse `_summarize_resource` for output shaping if your tool returns resources.
- Type hints on the function signature become the JSON-RPC input schema automatically (FastMCP handles this).

For aggregator tools (`triage` / `inventory` style), follow the pattern: build a list of `kubectl get -A -o json` arg vectors, fan out via `ThreadPoolExecutor(max_workers=8)`, post-process and group locally. CRD detection is graceful — wrap the per-kind `kubectl` call in a `try/except RuntimeError` and skip absent kinds silently.
