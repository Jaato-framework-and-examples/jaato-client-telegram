import asyncio
import json
import os
import shutil

TOOL_SCHEMA = {
    "name": "mercadona",
    "description": (
        "Search Mercadona's catalog, price items, and manage your cart via the "
        "mercadona-cli. Actions: 'search' (find products by name), 'batch' "
        "(resolve multiple items to IDs+prices), 'product' (get details for a "
        "specific product ID), 'price_list' (price all items on the shopping list), "
        "'cart_get' (show current cart), 'cart_add' (add product to cart), "
        "'cart_clear' (empty the cart), 'set_postal' (set warehouse from postal code), "
        "'status' (check CLI installation and auth state)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "search", "batch", "product", "price_list",
                    "cart_get", "cart_add", "cart_clear",
                    "set_postal", "status",
                ],
                "description": "What to do.",
            },
            "query": {
                "type": "string",
                "description": "Search term. Required for 'search'.",
            },
            "items": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of item names to resolve. Required for 'batch'.",
            },
            "product_id": {
                "type": "integer",
                "description": "Mercadona product ID. Required for 'product' and 'cart_add'.",
            },
            "quantity": {
                "type": "integer",
                "description": "Quantity for 'cart_add' (default 1).",
            },
            "postal_code": {
                "type": "string",
                "description": "Postal code for 'set_postal' (e.g. '28022').",
            },
            "limit": {
                "type": "integer",
                "description": "Max results for 'search' (default 5).",
            },
            "fresh": {
                "type": "boolean",
                "description": "For 'search': prefer fresh over frozen/canned (default false).",
            },
            "max_eur": {
                "type": "integer",
                "description": "Budget cap in euros for cart operations (optional).",
            },
        },
        "required": ["action"],
    },
}

SHOPPING_LIST_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "shopping_list.json"
)

def _find_cli():
    """Locate the mercadona CLI binary: explicit MERCADONA_BIN env override,
    else whatever is on PATH (no hardcoded machine-specific path)."""
    override = os.environ.get("MERCADONA_BIN")
    if override and os.path.isfile(os.path.expanduser(override)):
        return os.path.expanduser(override)
    return shutil.which("mercadona")


async def _run(args_list, timeout=30):
    """Run a mercadona CLI command and return (stdout, stderr, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        *args_list,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "", f"Command timed out after {timeout}s", -1
    return stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace"), proc.returncode


def _load_shopping_list():
    """Load the shopping list from the JSON file."""
    if os.path.exists(SHOPPING_LIST_FILE):
        with open(SHOPPING_LIST_FILE, "r") as f:
            return json.load(f)
    return []


def _format_search_results(data, limit=5):
    """Format search hits into a readable string."""
    hits = data.get("hits", [])
    if not hits:
        return "No products found."

    lines = []
    for i, h in enumerate(hits[:limit]):
        pid = h.get("objectID", "?")
        name = h.get("display_name", h.get("name", "Unknown"))
        price = h.get("price", "?")
        unit = h.get("unit_price", "")
        price_info = f"{price}€"
        if unit:
            price_info += f" ({unit}€)"
        lines.append(f"  [{pid}] {name} — {price_info}")

    total = data.get("nbHits", len(hits))
    header = f"Found {total} result(s). Top {min(limit, len(hits))}:"
    return header + "\n" + "\n".join(lines)


def _format_batch_output(stdout):
    """Parse and format batch command output."""
    return stdout.strip() if stdout.strip() else "No results."


def _format_product(data):
    """Format a single product's details."""
    if not isinstance(data, dict):
        return str(data)

    pid = data.get("id", "?")
    name = data.get("display_name", data.get("name", "Unknown"))
    price = data.get("price", "?")
    unit = data.get("unit_price", "")
    img = data.get("share_url", data.get("url", ""))

    lines = [f"[{pid}] {name}", f"  Price: {price}€"]
    if unit:
        lines.append(f"  Unit price: {unit}€")
    if img:
        lines.append(f"  URL: {img}")

    nutrition = data.get("nutrition_information") or {}
    allergens = nutrition.get("allergens", "")
    if allergens:
        lines.append(f"  Allergens: {allergens}")

    return "\n".join(lines)


async def execute(args, ctx):
    action = args["action"]
    cli = _find_cli()

    # --- STATUS ---
    if action == "status":
        if not cli:
            return {
                "text": (
                    "❌ mercadona CLI is not installed.\n"
                    "Install with:\n"
                    "  npm install -g @ivorpad/mercadona\n"
                    "or:\n"
                    "  curl -fsSL https://raw.githubusercontent.com/ivorpad/mercadona-cli/main/install.sh | sh"
                ),
            }

        stdout, stderr, rc = await _run([cli, "whoami"])
        if rc == 0 and "ok" in stdout.lower():
            auth_status = "✅ Authenticated"
        else:
            auth_status = "⚠️ Not authenticated (search/batch still work without auth)"

        return {
            "text": (
                f"✅ mercadona CLI installed at: {cli}\n"
                f"{auth_status}\n\n"
                f"Config dir: ~/.mercadona/"
            ),
        }

    if not cli:
        return {
            "text": (
                "❌ mercadona CLI is not installed. Cannot perform this action.\n"
                "Install with: npm install -g @ivorpad/mercadona"
            ),
        }

    # --- SEARCH ---
    if action == "search":
        query = args.get("query")
        if not query:
            return {"text": "Error: 'query' is required for search."}

        limit = args.get("limit", 5)
        cmd = [cli, "search", "--json", "--limit", str(limit), query]
        if args.get("fresh"):
            cmd.insert(3, "--fresh")

        stdout, stderr, rc = await _run(cmd)
        if rc != 0:
            return {"text": f"Search failed: {stderr.strip() or stdout.strip()}"}

        try:
            data = json.loads(stdout)
            return {"text": _format_search_results(data, limit)}
        except json.JSONDecodeError:
            return {"text": f"Search result (raw):\n{stdout.strip()}"}

    # --- BATCH ---
    if action == "batch":
        items = args.get("items", [])
        if not items:
            return {"text": "Error: 'items' is required for batch."}

        items_text = "\n".join(items)
        cmd = [cli, "batch", "-f", "-"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=items_text.encode()),
                timeout=30,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return {"text": "Batch command timed out."}

        stdout_str = stdout.decode("utf-8", errors="replace")
        stderr_str = stderr.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            return {"text": f"Batch failed: {stderr_str.strip() or stdout_str.strip()}"}

        return {"text": _format_batch_output(stdout_str)}

    # --- PRODUCT ---
    if action == "product":
        pid = args.get("product_id")
        if not pid:
            return {"text": "Error: 'product_id' is required for product."}

        cmd = [cli, "product", str(pid), "--json"]
        stdout, stderr, rc = await _run(cmd)
        if rc != 0:
            return {"text": f"Product lookup failed: {stderr.strip() or stdout.strip()}"}

        try:
            data = json.loads(stdout)
            return {"text": _format_product(data)}
        except json.JSONDecodeError:
            return {"text": f"Product info (raw):\n{stdout.strip()}"}

    # --- PRICE LIST ---
    if action == "price_list":
        shopping_items = _load_shopping_list()
        if not shopping_items:
            return {"text": "Your shopping list is empty. Nothing to price."}

        items_text = "\n".join(shopping_items)
        cmd = [cli, "batch", "-f", "-"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=items_text.encode()),
                timeout=30,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return {"text": "Batch command timed out."}

        stdout_str = stdout.decode("utf-8", errors="replace")
        stderr_str = stderr.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            return {
                "text": (
                    f"⚠️ Some items could not be found:\n{stderr_str.strip()}\n\n"
                    f"Successful matches:\n{stdout_str.strip()}"
                )
            }

        result = (
            f"🛒 Shopping list priced at Mercadona:\n\n"
            f"{stdout_str.strip()}\n\n"
            f"💡 Use 'cart_add' to add items to your Mercadona cart."
        )
        return {"text": result}

    # --- SET POSTAL ---
    if action == "set_postal":
        postal = args.get("postal_code")
        if not postal:
            return {"text": "Error: 'postal_code' is required for set_postal."}

        stdout, stderr, rc = await _run([cli, "set-postal", postal])
        if rc != 0:
            return {"text": f"Failed to set postal code: {stderr.strip() or stdout.strip()}"}

        return {"text": stdout.strip() or f"Warehouse set for postal code {postal}."}

    # --- CART GET ---
    if action == "cart_get":
        stdout, stderr, rc = await _run([cli, "cart", "get", "--json"])
        if rc != 0:
            return {"text": f"Failed to get cart: {stderr.strip() or stdout.strip()}"}

        try:
            data = json.loads(stdout)
            cart_items = data if isinstance(data, list) else data.get("items", [])
            if not cart_items:
                return {"text": "🛒 Your Mercadona cart is empty."}

            lines = ["🛒 Your Mercadona cart:"]
            for item in cart_items:
                name = item.get("display_name", item.get("name", "Unknown"))
                qty = item.get("quantity", "?")
                price = item.get("unit_price", "?")
                total = item.get("total_price", "?")
                lines.append(f"  • {name} — {qty} × {price}€ = {total}€")

            cart_total = data.get("total", "?") if isinstance(data, dict) else "?"
            if cart_total != "?":
                lines.append(f"\n  💰 Total: {cart_total}€")
            return {"text": "\n".join(lines)}
        except json.JSONDecodeError:
            return {"text": f"Cart (raw):\n{stdout.strip()}"}

    # --- CART ADD ---
    if action == "cart_add":
        pid = args.get("product_id")
        if not pid:
            return {"text": "Error: 'product_id' is required for cart_add."}

        qty = args.get("quantity", 1)
        cmd = [cli, "cart", "add", str(pid), str(qty)]
        max_eur = args.get("max_eur")
        if max_eur:
            cmd.extend(["--max", str(max_eur)])

        stdout, stderr, rc = await _run(cmd)
        if rc != 0:
            err = stderr.strip() or stdout.strip()
            if "BUDGET" in err:
                return {"text": f"🛑 {err}"}
            return {"text": f"Failed to add to cart: {err}"}

        return {"text": f"✅ Added product {pid} (×{qty}) to cart."}

    # --- CART CLEAR ---
    if action == "cart_clear":
        stdout, stderr, rc = await _run([cli, "cart", "clear"])
        if rc != 0:
            return {"text": f"Failed to clear cart: {stderr.strip() or stdout.strip()}"}

        return {"text": "🛒 Cart cleared."}

    return {"text": f"Unknown action: {action}"}
