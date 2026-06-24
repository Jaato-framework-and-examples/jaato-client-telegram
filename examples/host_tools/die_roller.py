# Example host tool — built by the jaato-client-telegram bot on user request via
# `register_tool` (self-extension). REFERENCE ONLY: real tools live in the bot's
# host_tools_dir (outside the repo, so the confined runner can't self-install).
# Copy/adapt this or build your own. See docs/features/host-tools.md.

import random
import re

TOOL_SCHEMA = {
    "name": "die_roller",
    "description": "Roll one or more dice. Supports standard notation like 'd20', '2d6', '1d20+3', or explicit sides/rolls parameters.",
    "parameters": {
        "type": "object",
        "properties": {
            "sides": {
                "type": "integer",
                "description": "Number of sides on the die (e.g. 20 for d20)."
            },
            "rolls": {
                "type": "integer",
                "description": "Number of dice to roll (default 1)."
            },
            "modifier": {
                "type": "integer",
                "description": "A number to add to the total (default 0)."
            },
            "notation": {
                "type": "string",
                "description": "Dice notation string like '2d6+3'. Parsed automatically; overrides sides/rolls/modifier."
            }
        }
    }
}


def _parse_notation(notation):
    m = re.fullmatch(r"(\d*)d(\d+)([+-]\d+)?", notation.strip().lower())
    if not m:
        raise ValueError(f"Invalid dice notation: {notation!r}. Use format like 2d6+3")
    rolls = int(m.group(1)) if m.group(1) else 1
    sides = int(m.group(2))
    modifier = int(m.group(3)) if m.group(3) else 0
    return rolls, sides, modifier


async def execute(args, ctx):
    try:
        if "notation" in args and args["notation"]:
            rolls, sides, modifier = _parse_notation(args["notation"])
        else:
            sides = args.get("sides", 20)
            rolls = args.get("rolls", 1)
            modifier = args.get("modifier", 0)

        if sides < 2:
            return {"error": "A die must have at least 2 sides."}
        if rolls < 1:
            return {"error": "Must roll at least 1 die."}
        if rolls > 100:
            return {"error": "Maximum 100 dice per roll."}

        individual = [random.randint(1, sides) for _ in range(rolls)]
        total = sum(individual) + modifier

        label = f"{rolls}d{sides}"
        if modifier > 0:
            label += f"+{modifier}"
        elif modifier < 0:
            label += str(modifier)

        if rolls == 1:
            detail = f"🎲 {label}: **{individual[0]}**"
            if modifier != 0:
                detail += f" → {total}"
        else:
            dice_str = ", ".join(str(d) for d in individual)
            detail = f"🎲 {label}: [{dice_str}]"
            if modifier != 0:
                detail += f" → {total}"
            else:
                detail += f" = **{total}**"

        return {"result": detail}
    except ValueError as e:
        return {"error": str(e)}
