
import json, io
from PIL import Image, ImageDraw, ImageFont

TOOL_SCHEMA = {
    "name": "ttt",
    "description": "Tic-tac-toe game. Actions: 'new' (start game), 'move' (place X/O at position 1-9). Sends board image with inline keyboard to Telegram.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["new", "move"], "description": "'new' starts a fresh game, 'move' plays at a position"},
            "position": {"type": "integer", "minimum": 1, "maximum": 9, "description": "Cell position 1-9 (1=top-left, 9=bottom-right). Required for 'move'."}
        },
        "required": ["action"]
    }
}

STATE_FILE = "/tmp/ttt_state.json"

def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def _save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def _draw_board(board, winner=None, is_draw=False):
    size = 400
    cell = size // 3
    img = Image.new("RGB", (size, size), "#1e1e2e")
    draw = ImageDraw.Draw(img)

    line_color = "#cdd6f4"
    w = 4
    for i in range(1, 3):
        draw.line([(i * cell, 10), (i * cell, size - 10)], fill=line_color, width=w)
        draw.line([(10, i * cell), (size - 10, i * cell)], fill=line_color, width=w)

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 80)
    except (OSError, IOError):
        font = ImageFont.load_default()

    for idx, mark in enumerate(board):
        if not mark:
            continue
        row, col = divmod(idx, 3)
        cx = col * cell + cell // 2
        cy = row * cell + cell // 2
        color = "#f38ba8" if mark == "X" else "#89b4fa"
        bbox = font.getbbox(mark)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2 - bbox[1]), mark, fill=color, font=font)

    if winner:
        win_lines = [
            [(0,0),(0,1),(0,2)], [(1,0),(1,1),(1,2)], [(2,0),(2,1),(2,2)],
            [(0,0),(1,0),(2,0)], [(0,1),(1,1),(2,1)], [(0,2),(1,2),(2,2)],
            [(0,0),(1,1),(2,2)], [(0,2),(1,1),(2,0)],
        ]
        for line in win_lines:
            if all(board[r*3+c] == winner for r, c in line):
                r1, c1 = line[0]
                r2, c2 = line[2]
                x1 = c1 * cell + cell // 2
                y1 = r1 * cell + cell // 2
                x2 = c2 * cell + cell // 2
                y2 = r2 * cell + cell // 2
                draw.line([(x1, y1), (x2, y2)], fill="#a6e3a1", width=6)
                break

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def _check_winner(board):
    lines = [
        [0,1,2],[3,4,5],[6,7,8],
        [0,3,6],[1,4,7],[2,5,8],
        [0,4,8],[2,4,6],
    ]
    for line in lines:
        vals = [board[i] for i in line]
        if vals[0] and vals.count(vals[0]) == 3:
            return vals[0]
    return None

def _build_keyboard(board, game_over=False):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = []
    labels = {1:"1️⃣",2:"2️⃣",3:"3️⃣",4:"4️⃣",5:"5️⃣",6:"6️⃣",7:"7️⃣",8:"8️⃣",9:"9️⃣"}
    for row in range(3):
        r = []
        for col in range(3):
            pos = row * 3 + col + 1
            idx = pos - 1
            if board[idx] or game_over:
                r.append(InlineKeyboardButton(
                    text=board[idx] if board[idx] else "·",
                    callback_data="ttt_ignore"
                ))
            else:
                r.append(InlineKeyboardButton(
                    text=labels[pos],
                    callback_data=f"ttt_move:{pos}"
                ))
        kb.append(r)
    kb.append([InlineKeyboardButton(text="🔄 New Game", callback_data="ttt_new")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

async def execute(args, ctx):
    from aiogram.types import BufferedInputFile
    action = args.get("action", "new")
    state = _load_state()

    if action == "new":
        board = [""] * 9
        turn = "X"
        msg = "🎮 New game! You are **X**, I am **O**. Your move!"
    elif action == "move":
        if not state:
            return {"error": "No active game. Start one with action='new'."}
        board = state["board"]
        turn = state["turn"]
        pos = args.get("position")
        if pos is None:
            return {"error": "Provide position (1-9)."}
        if board[pos - 1]:
            return {"error": f"Cell {pos} is already taken."}
        if turn != "X":
            return {"error": "Not your turn!"}
        board[pos - 1] = "X"
        winner = _check_winner(board)
        if winner:
            state = {"board": board, "turn": turn}
            _save_state(state)
            photo = BufferedInputFile(_draw_board(board, winner=winner), filename="ttt.png")
            kb = _build_keyboard(board, game_over=True)
            await ctx.bot.send_photo(ctx.chat_id, photo, caption="🎉 **You win!** Well played!", reply_markup=kb, parse_mode="Markdown")
            return {"result": "X wins!"}
        if all(board):
            state = {"board": board, "turn": turn}
            _save_state(state)
            photo = BufferedInputFile(_draw_board(board, is_draw=True), filename="ttt.png")
            kb = _build_keyboard(board, game_over=True)
            await ctx.bot.send_photo(ctx.chat_id, photo, caption="🤝 **Draw!** Evenly matched.", reply_markup=kb, parse_mode="Markdown")
            return {"result": "Draw!"}
        turn = "O"

        def _ai_move(b):
            for m in range(9):
                if not b[m]:
                    b[m] = "O"
                    if _check_winner(b) == "O":
                        b[m] = ""
                        return m
                    b[m] = ""
            for m in range(9):
                if not b[m]:
                    b[m] = "X"
                    if _check_winner(b) == "X":
                        b[m] = ""
                        return m
                    b[m] = ""
            if not b[4]:
                return 4
            for m in [0,2,6,8]:
                if not b[m]:
                    return m
            for m in range(9):
                if not b[m]:
                    return m
            return None

        ai = _ai_move(board)
        if ai is not None:
            board[ai] = "O"
        winner = _check_winner(board)
        if winner:
            state = {"board": board, "turn": turn}
            _save_state(state)
            photo = BufferedInputFile(_draw_board(board, winner=winner), filename="ttt.png")
            kb = _build_keyboard(board, game_over=True)
            await ctx.bot.send_photo(ctx.chat_id, photo, caption="🤖 **I win!** Better luck next time.", reply_markup=kb, parse_mode="Markdown")
            return {"result": "O wins!"}
        if all(board):
            state = {"board": board, "turn": turn}
            _save_state(state)
            photo = BufferedInputFile(_draw_board(board, is_draw=True), filename="ttt.png")
            kb = _build_keyboard(board, game_over=True)
            await ctx.bot.send_photo(ctx.chat_id, photo, caption="🤝 **Draw!** Evenly matched.", reply_markup=kb, parse_mode="Markdown")
            return {"result": "Draw!"}
        turn = "X"
        msg = "My turn: I played **O**. Your move!"

    state = {"board": board, "turn": turn}
    _save_state(state)
    photo = BufferedInputFile(_draw_board(board), filename="ttt.png")
    kb = _build_keyboard(board)
    await ctx.bot.send_photo(ctx.chat_id, photo, caption=msg, reply_markup=kb, parse_mode="Markdown")
    return {"result": f"Board sent. Turn: {turn}"}
