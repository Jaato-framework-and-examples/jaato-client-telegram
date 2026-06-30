import json, os

TOOL_SCHEMA = {
    "name": "shopping_list",
    "description": "Manage a persistent shopping list stored in the workspace. Actions: 'add' (append items), 'list' (show all items), 'remove' (delete specific items), 'clear' (wipe the whole list).",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "list", "remove", "clear"],
                "description": "What to do with the list."
            },
            "items": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Items to add or remove. Required for 'add' and 'remove'."
            }
        },
        "required": ["action"]
    }
}

DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "shopping_list.json")

def _load():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return []

def _save(lst):
    with open(DATA_FILE, "w") as f:
        json.dump(lst, f, indent=2)

async def execute(args, ctx):
    action = args["action"]
    items = args.get("items", [])
    lst = _load()

    if action == "add":
        for item in items:
            if item not in lst:
                lst.append(item)
        _save(lst)
        return {"text": f"Added {len(items)} item(s). List now has {len(lst)} item(s)."}

    elif action == "list":
        if not lst:
            return {"text": "Your shopping list is empty."}
        numbered = "\n".join(f"  {i+1}. {item}" for i, item in enumerate(lst))
        return {"text": f"Shopping list ({len(lst)} items):\n{numbered}"}

    elif action == "remove":
        removed = [item for item in items if item in lst]
        lst = [item for item in lst if item not in items]
        _save(lst)
        if removed:
            return {"text": f"Removed: {', '.join(removed)}"}
        return {"text": "None of those items were on the list."}

    elif action == "clear":
        count = len(lst)
        _save([])
        return {"text": f"Cleared {count} item(s). List is now empty."}

    return {"text": "Unknown action. Use: add, list, remove, clear."}
