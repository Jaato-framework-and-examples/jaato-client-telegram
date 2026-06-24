# Example host tool — built by the jaato-client-telegram bot on user request via
# `register_tool` (self-extension). REFERENCE ONLY: real tools live in the bot's
# host_tools_dir (outside the repo, so the confined runner can't self-install).
# Copy/adapt this or build your own. See docs/features/host-tools.md.

TOOL_SCHEMA = {
    "name": "reverse_text",
    "description": "Reverse a given text string.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to reverse."
            }
        },
        "required": ["text"]
    }
}

async def execute(args, ctx):
    text = args.get("text", "")
    return {"result": text[::-1]}
