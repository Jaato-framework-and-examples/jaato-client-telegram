import subprocess, json

TOOL_SCHEMA = {
    "name": "docker_status",
    "description": "Report the status of Docker containers. Shows running, stopped, and total counts with per-container details including name, image, status, ports, and uptime.",
    "parameters": {
        "type": "object",
        "properties": {
            "all": {
                "type": "boolean",
                "description": "Include stopped containers. Default false (running only).",
                "default": False
            },
            "filter_name": {
                "type": "string",
                "description": "Filter containers by name (substring match)."
            }
        }
    }
}


async def execute(args: dict, ctx) -> dict:
    all_containers = args.get("all", False)
    filter_name = args.get("filter_name", "")

    cmd = ["docker", "ps", "-a" if all_containers else "ps",
           "--format", "{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return {"error": "Docker is not installed or not in PATH."}
    except subprocess.TimeoutExpired:
        return {"error": "Docker command timed out."}

    if result.returncode != 0:
        return {"error": f"Docker error: {result.stderr.strip()}"}

    lines = [l for l in result.stdout.strip().splitlines() if l]
    if filter_name:
        lines = [l for l in lines if filter_name.lower() in l.lower()]

    containers = []
    for line in lines:
        parts = line.split("|", 4)
        if len(parts) < 4:
            continue
        cid, name, image, status = parts[:4]
        ports = parts[4] if len(parts) > 4 else ""
        containers.append({
            "id": cid[:12],
            "name": name,
            "image": image,
            "status": status,
            "ports": ports.strip(", ") if ports else "—"
        })

    running = sum(1 for c in containers if "Up" in c["status"])
    stopped = len(containers) - running

    summary = f"📦 **Docker Containers**: {running} running, {stopped} stopped ({len(containers)} total)\n\n"
    for c in containers:
        icon = "🟢" if "Up" in c["status"] else "🔴"
        ports_short = (c["ports"][:60] + "…") if len(c["ports"]) > 60 else c["ports"]
        summary += f"{icon} **{c['name']}**\n"
        summary += f"   Image: `{c['image']}`\n"
        summary += f"   Status: {c['status']}\n"
        if ports_short != "—":
            summary += f"   Ports: `{ports_short}`\n"
        summary += "\n"

    return {"result": summary}
