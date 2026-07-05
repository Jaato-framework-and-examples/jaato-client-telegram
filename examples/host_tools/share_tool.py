"""Contribute an installed host tool to the shared jaato store via a pull request.

Opens a PR (fork-and-PR) against
``Jaato-framework-and-examples/jaato-telegram-bot-tools-store`` proposing one of
this bot's INSTALLED tools. A human reviews + merges — that is the trust gate; the
registry regenerates on merge. The user is asked to confirm first (it's a PUBLIC
PR). Needs a GitHub token in ``JAATO_TOOLSTORE_GH_TOKEN`` (a machine account or a
fine-grained PAT that can fork + open PRs); the feature is OFF if unset.
See jaato-client-telegram/docs/design/tool-sharing-marketplace.md.
"""

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

TOOL_SCHEMA = {
    "name": "share_tool",
    "description": (
        "Propose one of your INSTALLED host tools to the shared jaato store by "
        "opening a pull request (a human reviews + merges before anyone can install "
        "it). Asks the user to confirm — it opens a PUBLIC PR whose code is visible. "
        "Requires a configured GitHub token."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The installed tool to contribute."},
            "note": {"type": "string", "description": "Optional note for the PR (what it does / why)."},
        },
        "required": ["name"],
    },
}
TOOL_DEPS = []  # stdlib only

_OWNER = "Jaato-framework-and-examples"
_REPO = "jaato-telegram-bot-tools-store"
_API = "https://api.github.com"
_SAFE = re.compile(r"^[a-z][a-z0-9_]*$")


def _token() -> str:
    return os.environ.get("JAATO_TOOLSTORE_GH_TOKEN", "")


def _wake_config() -> "tuple[str, str]":
    """(store PUBLIC signing key PEM, this bot's public wake-endpoint URL) from the
    env. Both empty ⇒ review-wake binding is OFF — no default, empty = disabled."""
    return (os.environ.get("JAATO_TOOLSTORE_WAKE_PUBKEY", ""),
            os.environ.get("JAATO_WAKE_PUBLIC_ENDPOINT", ""))


def _wake_ref(pr_number: int) -> str:
    """Opaque routing handle for this PR — the store's review-relay reconstructs the
    same string from the webhook payload (owner/repo/number). Session-chosen."""
    return f"github-pr:{_OWNER}/{_REPO}#{pr_number}"


async def _maybe_bind_wake(ctx, pr_number: int) -> str:
    """Upsert a wake binding so a REVIEW on this PR can wake the session to address
    it (session-scoped trust: we declare the store's PUBLIC key). OFF unless the
    store pubkey + this bot's public wake endpoint are configured AND the host wired
    ``ctx.bind_wake``. Returns a short note to append to the result message."""
    pubkey, endpoint = _wake_config()
    binder = getattr(ctx, "bind_wake", None)
    if not (pubkey and endpoint) or binder is None:
        return ""
    res = await binder(_wake_ref(pr_number), [pubkey]) or {}
    outcome = res.get("outcome", "")
    if outcome == "ok":
        return "\n\nReview comments on this PR will wake me to address them automatically."
    if outcome == "disabled":
        return ""
    return f"\n\n(Couldn't arm review-wake for this PR: {outcome}.)"


def _gh(method: str, path: str, token: str, body=None):
    """One GitHub REST call. Returns (status, json). Never raises on HTTP error."""
    url = path if path.startswith("http") else f"{_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "jaato-telegram-bot-tools",
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as r:  # noqa: S310 — fixed api host
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode() or "{}")
        except Exception:
            payload = {}
        return e.code, payload
    except Exception as e:  # noqa: BLE001 — network
        return 0, {"message": str(e)}


async def execute(args: dict, ctx) -> dict:
    name = (args.get("name") or "").strip()
    if not _SAFE.match(name):
        return {"error": f"Invalid tool name {name!r} (lowercase identifier)."}
    token = _token()
    if not token:
        return {"error": (
            "Contribution is disabled — no GitHub token configured. Set "
            "JAATO_TOOLSTORE_GH_TOKEN to a machine account / fine-grained PAT that "
            "can fork and open PRs on the store."
        )}
    htd = getattr(ctx, "host_tools_dir", "")
    if not htd:
        return {"error": "host_tools_dir isn't available — can't read the tool source."}
    src = Path(htd) / f"{name}.py"
    if not src.is_file():
        return {"error": f"No installed tool {name!r} to share."}
    source = src.read_text()

    branch = f"share-{name}"

    # Authenticate, and check whether THIS bot already has an OPEN PR for this
    # tool — so we can phrase the confirmation as "open new" vs "update existing"
    # before any public write. Both calls are read-only.
    st, me = _gh("GET", "/user", token)
    if st != 200 or "login" not in me:
        return {"error": f"GitHub auth failed ({st}) — check the token."}
    owner = me["login"]
    st2, prs = _gh(
        "GET", f"/repos/{_OWNER}/{_REPO}/pulls?head={owner}:{branch}&state=open", token,
    )
    existing = prs[0] if (st2 == 200 and isinstance(prs, list) and prs) else None

    # It's a PUBLIC PR (code visible) — confirm, phrased for the actual action.
    if existing:
        ans = await ctx.ask(
            f"Update your existing PUBLIC pull request (#{existing['number']}) for "
            f"'{name}' with the current code? The new code will be visible in the PR.",
            ["Yes, update the PR", "Cancel"], timeout=300,
        )
        ok = ans == "Yes, update the PR"
    else:
        ans = await ctx.ask(
            f"Open a PUBLIC pull request to contribute '{name}' to the jaato tool "
            f"store? Its code will be visible in the PR.",
            ["Yes, open the PR", "Cancel"], timeout=300,
        )
        ok = ans == "Yes, open the PR"
    if not ok:
        return {"result": "Cancelled — nothing pushed."}

    # Ensure a fork exists (GitHub creates it asynchronously — poll until ready).
    _gh("POST", f"/repos/{_OWNER}/{_REPO}/forks", token)
    fork = {}
    for _ in range(12):
        st, fork = _gh("GET", f"/repos/{owner}/{_REPO}", token)
        if st == 200 and fork.get("id"):
            break
        time.sleep(2)
    if not fork.get("id"):
        return {"error": "Your fork wasn't ready in time — try share_tool again in a moment."}
    base = fork.get("default_branch", "main")

    st, ref = _gh("GET", f"/repos/{owner}/{_REPO}/git/ref/heads/{base}", token)
    if st != 200:
        return {"error": f"Couldn't read the fork's base branch ({st})."}
    base_sha = ref["object"]["sha"]

    # 201 created / 422 already exists (reuse the branch)
    _gh("POST", f"/repos/{owner}/{_REPO}/git/refs", token,
        {"ref": f"refs/heads/{branch}", "sha": base_sha})

    path = f"tools/{name}.py"
    st, file_resp = _gh("GET", f"/repos/{owner}/{_REPO}/contents/{path}?ref={branch}", token)
    file_sha = file_resp.get("sha") if st == 200 and isinstance(file_resp, dict) else None

    put = {
        "message": f"Add tool: {name}",
        "content": base64.b64encode(source.encode()).decode(),
        "branch": branch,
    }
    if file_sha:
        put["sha"] = file_sha
    st, _ = _gh("PUT", f"/repos/{owner}/{_REPO}/contents/{path}", token, put)
    if st not in (200, 201):
        return {"error": f"Couldn't commit the tool to your fork ({st})."}

    # A PR already existed → the commit above updated it. Refresh its wake binding
    # (rotation/TTL renewal) and report (no new PR).
    if existing:
        wake_note = await _maybe_bind_wake(ctx, existing["number"])
        return {"result": (
            f"Updated your existing pull request for '{name}' with the latest code:\n"
            f"{existing['html_url']}\nThe maintainer will see the new commit." + wake_note
        )}

    note = (args.get("note") or "").strip()
    body = f"Proposes the `{name}` host tool for the store."
    if note:
        body += f"\n\n{note}"
    body += (
        f"\n\n_Contributed by @{owner} via a jaato Telegram bot. Model-authored — "
        f"please review the code before merging; the registry regenerates on merge._"
    )
    # Non-secret routing marker so the store's review-relay knows which daemon to
    # POST a wake to (the wake_ref it derives from the PR event itself). Embedded at
    # creation; only when review-wake is configured. The endpoint is gated by the
    # daemon's signature verify, so exposing the URL is safe.
    pubkey, endpoint = _wake_config()
    if pubkey and endpoint:
        body += f"\n\n<!-- jaato-wake endpoint={endpoint} -->"
    st, pr = _gh("POST", f"/repos/{_OWNER}/{_REPO}/pulls", token, {
        "title": f"Add tool: {name}",
        "head": f"{owner}:{branch}",
        "base": base,
        "body": body,
        "maintainer_can_modify": True,
    })
    if st == 201 and pr.get("html_url"):
        wake_note = await _maybe_bind_wake(ctx, pr["number"])
        return {"result": (
            f"Opened a pull request to contribute '{name}':\n{pr['html_url']}\n"
            f"A maintainer reviews the code and merges — then everyone can install it."
            + wake_note
        )}
    if st == 422:
        # A PR already exists for this head branch — but we just committed the new
        # code to that branch above, so the EXISTING PR is now updated. This is the
        # normal path when re-running share_tool to address review feedback. Find
        # the PR and report it as UPDATED (don't hand-roll the API — this did it).
        st2, prs = _gh(
            "GET", f"/repos/{_OWNER}/{_REPO}/pulls?head={owner}:{branch}&state=open", token,
        )
        if st2 == 200 and isinstance(prs, list) and prs:
            wake_note = await _maybe_bind_wake(ctx, prs[0]["number"])
            return {"result": (
                f"Updated the existing pull request for '{name}' with your latest "
                f"changes:\n{prs[0]['html_url']}\n"
                f"The maintainer will see the new commit." + wake_note
            )}
        return {"result": (
            f"Pushed your changes to the '{name}' branch — a pull request already "
            f"exists for it. Check {_OWNER}/{_REPO} → Pull requests."
        )}
    return {"error": f"Couldn't open the PR ({st}): {pr.get('message', '')}"}
