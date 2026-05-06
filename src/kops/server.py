"""kops — read-only kubectl helper exposed via MCP.

Five tools: k8s_get, k8s_describe, k8s_logs, k8s_events, k8s_triage.

All operations are strictly read-only. The verb passed to kubectl is hardcoded
in each tool function; user input only fills argument values, never the verb,
so there is no path to mutation even with malicious input.
"""

from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kops")


# ---------- validation ----------

NAME_RE = re.compile(r"^[a-zA-Z0-9._-]{1,253}$")
SELECTOR_RE = re.compile(r"^[a-zA-Z0-9._=,!\-/() ]{1,512}$")
SINCE_RE = re.compile(r"^\d+[smhd]$")
KIND_FALLBACK_RE = re.compile(r"^[a-z][a-z0-9.-]{0,62}$")

ALLOWED_KINDS = {
    "pod", "pods", "po",
    "svc", "service", "services",
    "deploy", "deployment", "deployments",
    "sts", "statefulset", "statefulsets",
    "ds", "daemonset", "daemonsets",
    "rs", "replicaset", "replicasets",
    "cm", "configmap", "configmaps",
    "secret", "secrets",
    "ns", "namespace", "namespaces",
    "node", "nodes", "no",
    "ingress", "ingresses", "ing",
    "gateway", "gateways", "gw",
    "virtualservice", "virtualservices", "vs",
    "destinationrule", "destinationrules", "dr",
    "event", "events", "ev",
    "job", "jobs",
    "cronjob", "cronjobs", "cj",
    "pvc", "persistentvolumeclaim", "persistentvolumeclaims",
    "pv", "persistentvolume", "persistentvolumes",
    "hpa", "horizontalpodautoscaler", "horizontalpodautoscalers",
    "sa", "serviceaccount", "serviceaccounts",
    "endpoints", "ep",
    "networkpolicy", "networkpolicies", "netpol",
}


def _validate_kind(kind: str) -> str:
    k = kind.lower().strip()
    if k in ALLOWED_KINDS:
        return k
    if KIND_FALLBACK_RE.match(k):
        return k
    raise ValueError(f"invalid kind: {kind!r}")


def _validate_name(name: str | None, field: str = "name") -> str | None:
    if name is None or name == "":
        return None
    if not NAME_RE.match(name):
        raise ValueError(f"invalid {field}: {name!r}")
    return name


def _validate_selector(selector: str | None) -> str | None:
    if selector is None or selector == "":
        return None
    if not SELECTOR_RE.match(selector):
        raise ValueError(f"invalid selector: {selector!r}")
    return selector


def _validate_since(since: str) -> str:
    if not SINCE_RE.match(since):
        raise ValueError(f"invalid since: {since!r} (expected like '30m', '1h', '7d')")
    return since


def _since_to_seconds(s: str) -> int:
    n = int(s[:-1])
    unit = s[-1]
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


# ---------- subprocess helper ----------


def _run_kubectl(args: list[str], timeout: int = 30) -> str:
    """Run `kubectl <args>`. Never uses shell. Returns stdout, raises on failure."""
    cmd = ["kubectl", *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"kubectl timed out after {timeout}s: {' '.join(args[:3])}...")
    except FileNotFoundError:
        raise RuntimeError("kubectl not found in PATH")
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"kubectl failed: {stderr}")
    return proc.stdout


def _ctx_args(context: str | None) -> list[str]:
    ctx = _validate_name(context, "context")
    return ["--context", ctx] if ctx else []


# ---------- formatting helpers ----------


def _age(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ""
    delta = datetime.now(timezone.utc) - dt
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _truncate(text: str, limit: int = 30000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[truncated; full output was {len(text)} chars, showing first {limit}]"


def _container_images(pod_template_spec: dict) -> list[dict]:
    """Extract container name + image pairs from a pod-template-like spec section.
    Accepts either a Pod's spec or a workload's spec.template.spec — both have `containers`.
    """
    out = []
    for c in (pod_template_spec.get("containers") or []):
        out.append({"container": c.get("name"), "image": c.get("image")})
    inits = pod_template_spec.get("initContainers") or []
    for c in inits:
        out.append({"container": c.get("name"), "image": c.get("image"), "init": True})
    return out


# Memory units. Order matters — binary suffixes must be checked before decimal
# (otherwise "Mi" gets matched as "M" first).
_MEM_UNITS = [
    ("Ei", 1024 ** 6), ("Pi", 1024 ** 5), ("Ti", 1024 ** 4),
    ("Gi", 1024 ** 3), ("Mi", 1024 ** 2), ("Ki", 1024),
    ("E", 10 ** 18), ("P", 10 ** 15), ("T", 10 ** 12),
    ("G", 10 ** 9), ("M", 10 ** 6), ("K", 10 ** 3),
]


def _parse_cpu(v: str | None) -> int:
    """Return millicores. K8s CPU: '100m'=100mc, '1'=1000mc, '0.5'=500mc."""
    if not v:
        return 0
    s = str(v).strip()
    if s.endswith("m"):
        try:
            return int(float(s[:-1]))
        except ValueError:
            return 0
    try:
        return int(float(s) * 1000)
    except ValueError:
        return 0


def _parse_mem(v: str | None) -> int:
    """Return bytes. Accepts Ki/Mi/Gi (binary), K/M/G (decimal), or plain bytes."""
    if not v:
        return 0
    s = str(v).strip()
    for unit, mult in _MEM_UNITS:
        if s.endswith(unit):
            try:
                return int(float(s[:-len(unit)]) * mult)
            except ValueError:
                return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _fmt_cpu(mc: int) -> str | None:
    """millicores → human string. None when 0 (signals 'not declared')."""
    if not mc:
        return None
    if mc < 1000:
        return f"{mc}m"
    cores = mc / 1000
    return str(int(cores)) if cores == int(cores) else f"{cores:.1f}"


def _fmt_mem(b: int) -> str | None:
    """bytes → human string in binary units."""
    if not b:
        return None
    if b >= 1024 ** 3:
        gi = b / 1024 ** 3
        return f"{int(gi)}Gi" if gi == int(gi) else f"{gi:.1f}Gi"
    if b >= 1024 ** 2:
        mi = b / 1024 ** 2
        return f"{int(mi)}Mi" if mi == int(mi) else f"{mi:.0f}Mi"
    if b >= 1024:
        return f"{b // 1024}Ki"
    return f"{b}"


def _aggregate_resources(pod_template_spec: dict) -> dict | None:
    """Sum resources.requests/limits across all main containers.

    Init containers are excluded — they don't run concurrently with the workload's
    steady state, so they don't add to standing resource footprint (k8s effectively
    uses max(init_resources, sum(main_resources)) for scheduling).

    Returns None when nothing was declared, so the caller can omit the field.
    """
    cs = pod_template_spec.get("containers") or []
    if not cs:
        return None
    req_cpu = req_mem = lim_cpu = lim_mem = 0
    seen_any = False
    for c in cs:
        r = c.get("resources") or {}
        req = r.get("requests") or {}
        lim = r.get("limits") or {}
        for v in (req.get("cpu"), req.get("memory"), lim.get("cpu"), lim.get("memory")):
            if v:
                seen_any = True
                break
        req_cpu += _parse_cpu(req.get("cpu"))
        req_mem += _parse_mem(req.get("memory"))
        lim_cpu += _parse_cpu(lim.get("cpu"))
        lim_mem += _parse_mem(lim.get("memory"))
    if not seen_any:
        return None
    out = {}
    requests = {k: v for k, v in (("cpu", _fmt_cpu(req_cpu)), ("memory", _fmt_mem(req_mem))) if v}
    limits = {k: v for k, v in (("cpu", _fmt_cpu(lim_cpu)), ("memory", _fmt_mem(lim_mem))) if v}
    if requests:
        out["requests"] = requests
    if limits:
        out["limits"] = limits
    return out or None
    return out


def _summarize_resource(item: dict) -> dict:
    """Extract key fields per kind. Avoids dumping full spec to keep token usage sane."""
    md = item.get("metadata") or {}
    kind = item.get("kind", "")
    base: dict = {
        "name": md.get("name"),
        "namespace": md.get("namespace"),
        "kind": kind,
        "age": _age(md.get("creationTimestamp")),
    }
    labels = md.get("labels") or {}
    if labels:
        base["labels"] = dict(list(labels.items())[:5])

    spec = item.get("spec") or {}
    status = item.get("status") or {}

    if kind == "Pod":
        cstats = status.get("containerStatuses") or []
        ready = sum(1 for c in cstats if c.get("ready"))
        total = len(cstats)
        restarts = sum(c.get("restartCount", 0) for c in cstats)
        reason = ""
        for c in cstats:
            waiting = (c.get("state") or {}).get("waiting") or {}
            if waiting.get("reason"):
                reason = waiting["reason"]
                break
        if not reason:
            reason = status.get("reason", "") or ""
        base.update({
            "phase": status.get("phase"),
            "ready": f"{ready}/{total}" if total else "",
            "restarts": restarts,
            "node": spec.get("nodeName"),
            "podIP": status.get("podIP"),
            "images": _container_images(spec),
            "containerCount": len(spec.get("containers") or []),
        })
        res = _aggregate_resources(spec)
        if res:
            base["resources"] = res
        if reason:
            base["reason"] = reason
    elif kind == "Service":
        lb_ingress = (status.get("loadBalancer") or {}).get("ingress") or []
        base.update({
            "type": spec.get("type"),
            "clusterIP": spec.get("clusterIP"),
            "externalIPs": spec.get("externalIPs", []),
            "loadBalancer": lb_ingress,
            "ports": [
                {"name": p.get("name"), "port": p.get("port"),
                 "targetPort": p.get("targetPort"), "protocol": p.get("protocol"),
                 "nodePort": p.get("nodePort")}
                for p in spec.get("ports", [])
            ],
        })
    elif kind == "Deployment":
        template_spec = ((spec.get("template") or {}).get("spec")) or {}
        base.update({
            "desired": spec.get("replicas"),
            "available": status.get("availableReplicas", 0),
            "updated": status.get("updatedReplicas", 0),
            "ready": status.get("readyReplicas", 0),
            "images": _container_images(template_spec),
            "containerCount": len(template_spec.get("containers") or []),
        })
        res = _aggregate_resources(template_spec)
        if res:
            base["resources"] = res
    elif kind in ("StatefulSet", "DaemonSet", "ReplicaSet"):
        template_spec = ((spec.get("template") or {}).get("spec")) or {}
        base.update({
            "desired": spec.get("replicas") or status.get("desiredNumberScheduled"),
            "ready": status.get("readyReplicas") or status.get("numberReady"),
            "images": _container_images(template_spec),
            "containerCount": len(template_spec.get("containers") or []),
        })
        res = _aggregate_resources(template_spec)
        if res:
            base["resources"] = res
    elif kind == "Node":
        conds = {c["type"]: c["status"] for c in (status.get("conditions") or [])}
        base.update({
            "ready": conds.get("Ready"),
            "kubeletVersion": (status.get("nodeInfo") or {}).get("kubeletVersion"),
            "internalIP": next(
                (a["address"] for a in (status.get("addresses") or []) if a["type"] == "InternalIP"),
                None,
            ),
        })
        pressures = [t for t in ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")
                     if conds.get(t) == "True"]
        if pressures:
            base["pressures"] = pressures
    elif kind == "Ingress":
        base["hosts"] = [r.get("host") for r in spec.get("rules", []) if r.get("host")]

    return base


# ---------- tools ----------


@mcp.tool()
def k8s_get(
    kind: str,
    namespace: str | None = None,
    name: str | None = None,
    selector: str | None = None,
    context: str | None = None,
) -> list[dict]:
    """List or fetch K8s resources, returning summarized key fields per resource.

    Use this to scan resources or fetch a specific one by name. For deeper
    detail (events, conditions, container info), use k8s_describe.

    Args:
        kind: Resource kind. Common: pod, svc, deploy, sts, ds, cm, secret, ns,
              node, ingress, gateway, virtualservice, destinationrule, hpa, pvc.
        namespace: Target namespace; omit to scan all namespaces.
        name: Specific resource name; omit to list multiple.
        selector: K8s label selector, e.g. "app=foo,env=prod".
        context: kubeconfig context to use; defaults to current context.
    """
    k = _validate_kind(kind)
    ns = _validate_name(namespace, "namespace")
    nm = _validate_name(name, "name")
    sel = _validate_selector(selector)

    args = ["get", k]
    if nm:
        args.append(nm)
    if ns:
        args += ["-n", ns]
    elif nm is None:
        args.append("-A")
    if sel:
        args += ["-l", sel]
    args += ["-o", "json"]
    args += _ctx_args(context)

    out = _run_kubectl(args)
    payload = json.loads(out)
    if isinstance(payload, dict) and payload.get("kind", "").endswith("List"):
        items = payload.get("items", [])
    else:
        items = [payload]
    return [_summarize_resource(it) for it in items]


@mcp.tool()
def k8s_describe(
    kind: str,
    name: str,
    namespace: str | None = None,
    context: str | None = None,
) -> str:
    """Describe a single K8s resource (text output from `kubectl describe`).

    Use when k8s_get isn't enough — describe shows events, conditions,
    container details, volume mounts, image pull state, etc. Output is
    human-readable text, not JSON.

    Args:
        kind: Resource kind.
        name: Resource name (required).
        namespace: Target namespace.
        context: kubeconfig context.
    """
    k = _validate_kind(kind)
    nm = _validate_name(name, "name")
    if nm is None:
        raise ValueError("name is required")
    ns = _validate_name(namespace, "namespace")

    args = ["describe", k, nm]
    if ns:
        args += ["-n", ns]
    args += _ctx_args(context)

    out = _run_kubectl(args)
    return _truncate(out, 30000)


@mcp.tool()
def k8s_logs(
    pod: str,
    namespace: str | None = None,
    container: str | None = None,
    tail: int = 100,
    since: str | None = None,
    previous: bool = False,
    context: str | None = None,
) -> str:
    """Fetch logs from a pod.

    Args:
        pod: Pod name (required).
        namespace: Target namespace.
        container: Container name in multi-container pods.
        tail: Lines from the tail (default 100, hard max 1000).
        since: Look-back window like "5m", "1h"; only logs newer than this.
        previous: If True, fetch the previous container instance's logs (post-crash).
        context: kubeconfig context.
    """
    pd = _validate_name(pod, "pod")
    if pd is None:
        raise ValueError("pod is required")
    ns = _validate_name(namespace, "namespace")
    cn = _validate_name(container, "container")
    tail_clamped = max(1, min(int(tail), 1000))

    args = ["logs", pd]
    if ns:
        args += ["-n", ns]
    if cn:
        args += ["-c", cn]
    args.append(f"--tail={tail_clamped}")
    if since:
        _validate_since(since)
        args.append(f"--since={since}")
    if previous:
        args.append("--previous")
    args += _ctx_args(context)

    out = _run_kubectl(args)
    return _truncate(out, 50000)


@mcp.tool()
def k8s_events(
    namespace: str | None = None,
    kind: str | None = None,
    name: str | None = None,
    since: str = "30m",
    context: str | None = None,
) -> list[dict]:
    """List recent K8s events, most recent first.

    Use this to surface scheduling failures, image pull problems, OOMKilled,
    network issues, etc. Filter by namespace and/or involved object.

    Args:
        namespace: Target namespace; omit for cluster-wide.
        kind: Filter by involvedObject.kind (e.g. "Pod").
        name: Filter by involvedObject.name (use with `kind`).
        since: Look-back window (default "30m"). Format: "Ns", "Nm", "Nh", "Nd".
        context: kubeconfig context.
    """
    _validate_since(since)
    ns = _validate_name(namespace, "namespace")
    k = _validate_kind(kind) if kind else None
    nm = _validate_name(name, "name")

    args = ["get", "events", "-o", "json"]
    if ns:
        args += ["-n", ns]
    else:
        args.append("-A")
    selectors = []
    if k:
        selectors.append(f"involvedObject.kind={k.capitalize()}")
    if nm:
        selectors.append(f"involvedObject.name={nm}")
    if selectors:
        args += ["--field-selector", ",".join(selectors)]
    args += _ctx_args(context)

    out = _run_kubectl(args)
    payload = json.loads(out)
    items = payload.get("items", [])

    cutoff_s = _since_to_seconds(since)
    now = datetime.now(timezone.utc)

    rows = []
    for ev in items:
        ts = (
            ev.get("lastTimestamp")
            or ev.get("eventTime")
            or (ev.get("metadata") or {}).get("creationTimestamp")
        )
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - dt).total_seconds() > cutoff_s:
            continue
        obj = ev.get("involvedObject") or {}
        rows.append({
            "time": ts,
            "type": ev.get("type"),
            "reason": ev.get("reason"),
            "object": f"{obj.get('kind')}/{obj.get('name')}" if obj.get("kind") else "",
            "namespace": (ev.get("metadata") or {}).get("namespace"),
            "message": ev.get("message"),
            "count": ev.get("count", 1),
        })
    rows.sort(key=lambda r: r["time"], reverse=True)
    return rows[:200]


@mcp.tool()
def k8s_triage(
    namespace: str | None = None,
    since: str = "1h",
    context: str | None = None,
) -> dict:
    """⭐ Start here for cluster diagnostics. Single call returns:
    problem pods, recent warning events, unhealthy nodes, and stale deployments.

    Use this as the first tool when asked broad questions like "what's wrong
    with this cluster", "anything broken", or "give me a health summary".
    Then dig deeper with k8s_describe / k8s_logs / k8s_events for specifics.

    Args:
        namespace: Limit scope to a single namespace; omit for cluster-wide.
        since: Event recency window (default "1h"). Format: "Ns", "Nm", "Nh", "Nd".
        context: kubeconfig context.
    """
    _validate_since(since)
    ns = _validate_name(namespace, "namespace")
    ctx = _ctx_args(context)
    ns_args = ["-n", ns] if ns else ["-A"]

    def fetch(args: list[str]) -> dict:
        return json.loads(_run_kubectl(args))

    pods_args = ["get", "pod", *ns_args, "-o", "json", *ctx]
    events_args = ["get", "events", *ns_args, "-o", "json", *ctx]
    nodes_args = ["get", "node", "-o", "json", *ctx]
    deploys_args = ["get", "deploy", *ns_args, "-o", "json", *ctx]

    with ThreadPoolExecutor(max_workers=4) as ex:
        f_pods = ex.submit(fetch, pods_args)
        f_events = ex.submit(fetch, events_args)
        f_nodes = ex.submit(fetch, nodes_args) if namespace is None else None
        f_deploys = ex.submit(fetch, deploys_args)

        pods_payload = f_pods.result()
        events_payload = f_events.result()
        nodes_payload = f_nodes.result() if f_nodes else {"items": []}
        deploys_payload = f_deploys.result()

    problem_reasons = {
        "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
        "CreateContainerConfigError", "CreateContainerError",
        "InvalidImageName", "ContainerCannotRun", "RunContainerError",
    }
    problem_pods = []
    for p in pods_payload.get("items", []):
        st = p.get("status") or {}
        phase = st.get("phase", "")
        cstats = st.get("containerStatuses") or []
        problematic = phase not in ("Running", "Succeeded")
        reason = ""
        for c in cstats:
            waiting = (c.get("state") or {}).get("waiting") or {}
            r = waiting.get("reason")
            if r in problem_reasons:
                problematic = True
                reason = r
                break
            term = (c.get("lastState") or {}).get("terminated") or {}
            if term.get("reason") in {"OOMKilled", "Error"} and c.get("restartCount", 0) > 0:
                problematic = True
                reason = term["reason"]
        if problematic:
            md = p.get("metadata") or {}
            problem_pods.append({
                "name": md.get("name"),
                "namespace": md.get("namespace"),
                "phase": phase,
                "reason": reason or st.get("reason", ""),
                "restarts": sum(c.get("restartCount", 0) for c in cstats),
                "age": _age(md.get("creationTimestamp")),
                "node": (p.get("spec") or {}).get("nodeName"),
            })

    cutoff_s = _since_to_seconds(since)
    now = datetime.now(timezone.utc)
    warning_events = []
    for ev in events_payload.get("items", []):
        if ev.get("type") != "Warning":
            continue
        ts = (
            ev.get("lastTimestamp")
            or ev.get("eventTime")
            or (ev.get("metadata") or {}).get("creationTimestamp")
        )
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - dt).total_seconds() > cutoff_s:
            continue
        obj = ev.get("involvedObject") or {}
        warning_events.append({
            "time": ts,
            "reason": ev.get("reason"),
            "object": f"{obj.get('kind')}/{obj.get('name')}" if obj.get("kind") else "",
            "namespace": (ev.get("metadata") or {}).get("namespace"),
            "message": ev.get("message"),
            "count": ev.get("count", 1),
        })
    warning_events.sort(key=lambda r: r["time"], reverse=True)

    unhealthy_nodes = []
    for n in nodes_payload.get("items", []):
        conds = {c["type"]: c["status"] for c in (n.get("status", {}).get("conditions") or [])}
        ready = conds.get("Ready")
        pressures = [t for t in ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")
                     if conds.get(t) == "True"]
        if ready != "True" or pressures:
            md = n.get("metadata") or {}
            unhealthy_nodes.append({
                "name": md.get("name"),
                "ready": ready,
                "pressures": pressures,
                "kubeletVersion": (n.get("status", {}).get("nodeInfo") or {}).get("kubeletVersion"),
            })

    stale_deploys = []
    for d in deploys_payload.get("items", []):
        spec = d.get("spec") or {}
        status = d.get("status") or {}
        desired = spec.get("replicas", 0) or 0
        available = status.get("availableReplicas", 0) or 0
        progressing = next(
            (c for c in (status.get("conditions") or []) if c.get("type") == "Progressing"),
            None,
        )
        progressing_status = progressing.get("status") if progressing else None
        if available != desired or progressing_status not in (None, "True"):
            md = d.get("metadata") or {}
            stale_deploys.append({
                "name": md.get("name"),
                "namespace": md.get("namespace"),
                "desired": desired,
                "available": available,
                "ready": status.get("readyReplicas", 0),
                "progressing": progressing_status,
            })

    truncated = len(problem_pods) > 50 or len(warning_events) > 30

    return {
        "summary": {
            "problem_pods": len(problem_pods),
            "warning_events": len(warning_events),
            "unhealthy_nodes": len(unhealthy_nodes),
            "stale_deployments": len(stale_deploys),
            "truncated": truncated,
            "scope": f"namespace={ns}" if ns else "cluster-wide",
            "since": since,
        },
        "pods": problem_pods[:50],
        "events": warning_events[:30],
        "nodes": unhealthy_nodes,
        "deployments": stale_deploys,
    }


# Mapping from kubectl singular kind → output key on namespace entry.
_INVENTORY_KIND_KEYS = {
    "deployment": "deployments",
    "statefulset": "statefulsets",
    "daemonset": "daemonsets",
    "service": "services",
    "ingress": "ingresses",
    "horizontalpodautoscaler": "hpa",
    "persistentvolumeclaim": "pvcs",
    "configmap": "configmaps",
    "secret": "secrets",
    "job": "jobs",
    "cronjob": "cronjobs",
    "gateways.networking.istio.io": "istio_gateways",
    "virtualservices.networking.istio.io": "istio_virtualservices",
    "destinationrules.networking.istio.io": "istio_destinationrules",
}

_INVENTORY_DEFAULT_KINDS = [
    "deployment", "statefulset", "daemonset",
    "service", "ingress", "horizontalpodautoscaler",
    "persistentvolumeclaim",
    "configmap", "secret",
    "job", "cronjob",
]

_INVENTORY_ISTIO_KINDS = [
    "gateways.networking.istio.io",
    "virtualservices.networking.istio.io",
    "destinationrules.networking.istio.io",
]


@mcp.tool()
def k8s_inventory(
    namespace: str | None = None,
    include_istio: bool = True,
    context: str | None = None,
) -> dict:
    """⭐ One-shot comprehensive cluster snapshot. Use for documentation, audits,
    or any task that needs broad visibility — replaces ~50 individual k8s_get calls.

    Returns nodes (cluster-scoped) and per-namespace inventory of: deployments,
    statefulsets, daemonsets, services, ingresses, hpa, pvc, configmaps, secrets,
    jobs, cronjobs. Each item is summarized (key fields only, no full spec dump).

    Istio CRDs (Gateway / VirtualService / DestinationRule) are auto-included
    when present; absent CRDs are silently skipped. Set include_istio=False to skip.

    Pods are NOT included in the snapshot (potentially huge); only per-namespace
    pod counts are surfaced in the summary. Use k8s_triage for pod health,
    k8s_get pod for specifics.

    ConfigMap data and Secret values are NEVER returned — only metadata.

    Args:
        namespace: Limit scope to a single namespace; omit for cluster-wide.
        include_istio: Auto-detect and include Istio CRDs (default True).
        context: kubeconfig context.
    """
    ns = _validate_name(namespace, "namespace")
    ctx = _ctx_args(context)
    ns_args = ["-n", ns] if ns else ["-A"]

    namespaced_kinds = list(_INVENTORY_DEFAULT_KINDS)
    if include_istio:
        namespaced_kinds.extend(_INVENTORY_ISTIO_KINDS)

    def fetch(args: list[str]) -> dict | None:
        try:
            return json.loads(_run_kubectl(args))
        except RuntimeError:
            # CRD not installed, namespace gone, etc. Return None and skip.
            return None

    pods_args = ["get", "pod", *ns_args, "-o", "json", *ctx]
    namespaces_args = ["get", "namespace", "-o", "json", *ctx]
    nodes_args = ["get", "node", "-o", "json", *ctx]

    with ThreadPoolExecutor(max_workers=8) as ex:
        f_namespaces = ex.submit(fetch, namespaces_args)
        f_nodes = ex.submit(fetch, nodes_args) if ns is None else None
        f_pods = ex.submit(fetch, pods_args)
        kind_futures = {
            kind: ex.submit(fetch, ["get", kind, *ns_args, "-o", "json", *ctx])
            for kind in namespaced_kinds
        }

        namespaces_payload = f_namespaces.result() or {"items": []}
        nodes_payload = f_nodes.result() if f_nodes else {"items": []}
        pods_payload = f_pods.result() or {"items": []}
        kind_payloads = {kind: f.result() for kind, f in kind_futures.items()}

    items_by_ns_kind: dict[str, dict[str, list[dict]]] = {}
    by_kind_counts: dict[str, int] = {}
    istio_present = False
    for kind, payload in kind_payloads.items():
        if payload is None:
            continue
        items = payload.get("items", [])
        out_key = _INVENTORY_KIND_KEYS.get(kind, kind)
        if kind in _INVENTORY_ISTIO_KINDS and items:
            istio_present = True
        by_kind_counts[out_key] = len(items)
        for item in items:
            item_ns = (item.get("metadata") or {}).get("namespace") or ""
            items_by_ns_kind.setdefault(item_ns, {}).setdefault(out_key, []).append(
                _summarize_resource(item)
            )

    pods_per_ns: dict[str, int] = {}
    for p in pods_payload.get("items", []):
        item_ns = (p.get("metadata") or {}).get("namespace") or ""
        pods_per_ns[item_ns] = pods_per_ns.get(item_ns, 0) + 1

    nodes_summary = [_summarize_resource(n) for n in nodes_payload.get("items", [])]

    namespace_entries: list[dict] = []
    for ns_item in namespaces_payload.get("items", []):
        nm = (ns_item.get("metadata") or {}).get("name")
        if ns and nm != ns:
            continue
        entry = _summarize_resource(ns_item)
        entry["pods"] = pods_per_ns.get(nm, 0)
        for out_key, items in items_by_ns_kind.get(nm, {}).items():
            entry[out_key] = items
        namespace_entries.append(entry)

    namespace_entries.sort(key=lambda e: e["name"])

    return {
        "summary": {
            "scope": f"namespace={ns}" if ns else "cluster-wide",
            "namespaces": len(namespace_entries),
            "nodes": len(nodes_summary),
            "pods_total": sum(pods_per_ns.values()),
            "by_kind_counts": by_kind_counts,
            "istio_present": istio_present,
            "include_istio": include_istio,
        },
        "nodes": nodes_summary,
        "namespaces": namespace_entries,
    }


def main() -> None:
    """Entry point — runs the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
