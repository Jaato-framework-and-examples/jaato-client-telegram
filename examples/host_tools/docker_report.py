"""Custom tool: Docker container status reporter with health awareness."""

import re
import subprocess
import json

TOOL_SCHEMA = {
    "name": "docker_report",
    "description": (
        "Report Docker container status with health-check awareness. "
        "Shows a summary (running/stopped/unhealthy counts) followed by a "
        "compact per-container list with name, status, health, and ports. "
        "Can filter by name substring and optionally include stopped containers."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filter_name": {
                "type": "string",
                "description": "Filter containers by name (substring match).",
            },
            "include_stopped": {
                "type": "boolean",
                "description": "Include stopped/exited containers. Default false.",
                "default": False,
            },
        },
    },
}


def _run(cmd: str) -> str:
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _parse_containers(filter_name: str | None, include_stopped: bool) -> list[dict]:
    args = "docker ps --format '{{json .}}' --no-trunc"
    if include_stopped:
        args += " --all"
    raw = _run(args)
    if not raw:
        return []

    containers = []
    for line in raw.splitlines():
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        if filter_name and filter_name.lower() not in c.get("Names", "").lower():
            continue
        containers.append(c)
    return containers


_HEALTH_RE = re.compile(r"\s*\((?:healthy|unhealthy|starting)\)")


def _health_label(c: dict) -> str:
    status = c.get("Status", "")
    if "(healthy)" in status:
        return "healthy"
    if "(unhealthy)" in status:
        return "unhealthy"
    if "(starting)" in status:
        return "starting"
    return ""


def _clean_status(status: str) -> str:
    return _HEALTH_RE.sub("", status).strip()


def _format_ports(c: dict) -> str:
    ports = c.get("Ports", "")
    if not ports:
        return ""
    published = []
    for p in ports.split(", "):
        p = p.strip()
        if "->" not in p:
            continue
        published.append(p)
    return ", ".join(published) if published else ""


def _build_report(containers: list[dict]) -> str:
    if not containers:
        return "📦 No Docker containers found."

    running = 0
    stopped = 0
    unhealthy = 0
    lines: list[str] = []

    for c in sorted(containers, key=lambda x: x.get("Names", "")):
        name = c.get("Names", "?")
        raw_status = c.get("Status", "?")
        health = _health_label(c)
        status = _clean_status(raw_status)
        ports = _format_ports(c)

        if "Exited" in raw_status or "Dead" in raw_status:
            stopped += 1
            icon = "⛔"
        elif health == "unhealthy":
            unhealthy += 1
            icon = "🟡"
        elif health == "healthy":
            running += 1
            icon = "🟢"
        else:
            running += 1
            icon = "🔵"

        entry = f"{icon} **{name}** — {status}"
        if health:
            entry += f" ({health})"
        if ports:
            entry += f"\n   ↳ {ports}"
        lines.append(entry)

    summary = f"📦 **Docker**: {running} running"
    if stopped:
        summary += f", {stopped} stopped"
    if unhealthy:
        summary += f", {unhealthy} ⚠️ unhealthy"
    summary += f" ({running + stopped} total)"

    return summary + "\n\n" + "\n".join(lines)


async def execute(args: dict, ctx) -> dict:
    filter_name = args.get("filter_name")
    include_stopped = args.get("include_stopped", False)

    containers = _parse_containers(filter_name, include_stopped)
    report = _build_report(containers)

    return {"result": report}
