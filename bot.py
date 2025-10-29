#!/usr/bin/env python3
"""
Telegram Bracket Bot — single-elimination tournaments with random draw.

Features
- /start — intro
- /new <name> — start a fresh tournament for this chat
- /add <team1>; <team2>; ... — add one or many teams (semicolon or newline separated)
- /list — list teams
- /draw — randomize teams and create bracket (adds BYE if needed)
- /pairs — show first-round pairs
- /bracket — show the full bracket (text tree)
- /export — CSV export of the draw
- /reset — wipe current tournament
- /help — help

Persistence: JSON file per chat stored in ./data.json

Requires: python-telegram-bot>=20.0

Run:
  export BOT_TOKEN=123:ABC
  pip install python-telegram-bot==20.7
  python bot.py
"""

import os
import json
import math
import csv
import io
import random
from datetime import datetime
from typing import Dict, List, Tuple, Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# Allow custom storage path for Railway/containers (mount a Volume at /data)
DATA_FILE = os.getenv("DATA_FILE", "./data/data.json")

# -----------------------------
# Storage helpers
# -----------------------------

def load_db() -> Dict[str, Any]:
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_db(db: Dict[str, Any]) -> None:
    # Ensure parent folder exists
    os.makedirs(os.path.dirname(DATA_FILE) or ".", exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def get_chat(db: Dict[str, Any], chat_id: int) -> Dict[str, Any]:
    key = str(chat_id)
    if key not in db:
        db[key] = {
            "name": None,
            "teams": [],
            "bracket": None,  # list of rounds; each round is list of pair tuples
            "created_at": datetime.utcnow().isoformat(),
        }
    return db[key]

# -----------------------------
# Bracket logic
# -----------------------------

Pair = Tuple[str, str]
Round = List[Pair]


def next_power_of_two(n: int) -> int:
    if n < 1:
        return 1
    return 1 << (n - 1).bit_length()


def build_first_round(teams: List[str]) -> Round:
    pairs: Round = []
    for i in range(0, len(teams), 2):
        a = teams[i]
        b = teams[i + 1] if i + 1 < len(teams) else "BYE"
        pairs.append((a, b))
    return pairs


def build_full_bracket(seed_teams: List[str]) -> List[Round]:
    # Ensure power-of-two by adding BYEs
    n = len(seed_teams)
    target = next_power_of_two(n)
    byes = target - n
    teams = seed_teams[:] + ["BYE"] * byes
    # Shuffle already performed before calling typically, but keep order
    r1 = build_first_round(teams)
    rounds: List[Round] = [r1]

    # Build placeholder rounds
    matches_in_round = len(r1)
    while matches_in_round > 1:
        next_round: Round = []
        for i in range(0, matches_in_round, 2):
            m1 = i + 1
            m2 = i + 2
            left = f"Winner of R{len(rounds)}M{m1}"
            right = f"Winner of R{len(rounds)}M{m2}"
            next_round.append((left, right))
        rounds.append(next_round)
        matches_in_round = len(next_round)
    return rounds


def render_pairs_text(pairs: Round, round_no: int) -> str:
    lines = [f"*Round {round_no}*:"]
    for idx, (a, b) in enumerate(pairs, start=1):
        vs = f"{a} — {b}"
        lines.append(f"M{idx}. {vs}")
    return "\n".join(lines)


def render_bracket_tree(rounds: List[Round]) -> str:
    # Text-only bracket representation
    parts: List[str] = []
    for i, rnd in enumerate(rounds, start=1):
        parts.append(render_pairs_text(rnd, i))
        if i != len(rounds):
            parts.append("")
    return "\n".join(parts)


# -----------------------------
# Bot command handlers
# -----------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Ассалом! Я помогу собрать сетку на выбывание.\n\n"
        "Команды:\n"
        "/new <название> — создать турнир\n"
        "/add <команда1>; <команда2>; ... — добавить команды\n"
        "/list — показать список команд\n"
        "/draw — случайная жеребьёвка и создание сетки\n"
        "/pairs — пары 1-го раунда\n"
        "/bracket — вся сетка (текст)\n"
        "/export — CSV-файл с сеткой\n"
        "/reset — очистить турнир\n"
        "/help — помощь",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = load_db()
    chat = get_chat(db, update.effective_chat.id)
    name = " ".join(context.args).strip() or f"Tournament {datetime.utcnow():%Y-%m-%d}"
    chat["name"] = name
    chat["teams"] = []
    chat["bracket"] = None
    save_db(db)
    await update.message.reply_text(f"Создан турнир: *{name}*. Добавьте команды через /add", parse_mode=ParseMode.MARKDOWN)


def _normalize_teams(raw: str) -> List[str]:
    # split by semicolon, comma or newline
    parts = [p.strip() for p in raw.replace("\n", ";").replace(",", ";").split(";")]
    teams = [p for p in parts if p]
    # dedupe while keeping order
    seen = set()
    uniq: List[str] = []
    for t in teams:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    return uniq


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = load_db()
    chat = get_chat(db, update.effective_chat.id)
    if not context.args and not update.message.text.partition(" ")[2].strip():
        await update.message.reply_text("Укажите команды после /add через ';' или с новой строки.")
        return
    raw = update.message.text.partition(" ")[2]
    new = _normalize_teams(raw)
    if not new:
        await update.message.reply_text("Не удалось распознать названия.")
        return
    before = len(chat["teams"])
    # Append while avoiding dups (case-insensitive)
    existing_low = {t.lower() for t in chat["teams"]}
    for t in new:
        if t.lower() not in existing_low:
            chat["teams"].append(t)
    save_db(db)
    added = len(chat["teams"]) - before
    await update.message.reply_text(f"Добавлено команд: *{added}*. Всего: *{len(chat['teams'])}*.", parse_mode=ParseMode.MARKDOWN)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = load_db()
    chat = get_chat(db, update.effective_chat.id)
    teams = chat["teams"]
    if not teams:
        await update.message.reply_text("Список пуст. Добавьте команды через /add.")
        return
    lines = [f"*Команды* ({len(teams)}):"] + [f"• {t}" for t in teams]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_draw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = load_db()
    chat = get_chat(db, update.effective_chat.id)
    teams = chat["teams"]
    if len(teams) < 2:
        await update.message.reply_text("Нужно минимум 2 команды.")
        return
    seed = random.randint(100000, 999999)
    random.Random(seed).shuffle(teams)
    rounds = build_full_bracket(teams)
    chat["bracket"] = {
        "seed": seed,
        "rounds": rounds,
    }
    save_db(db)
    r1_text = render_pairs_text(rounds[0], 1)
    await update.message.reply_text(
        f"Сетка создана. Seed: `{seed}`\n\n{r1_text}", parse_mode=ParseMode.MARKDOWN
    )


async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = load_db()
    chat = get_chat(db, update.effective_chat.id)
    br = chat.get("bracket")
    if not br:
        await update.message.reply_text("Сетка ещё не создана. Используйте /draw.")
        return
    text = render_pairs_text(br["rounds"][0], 1)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_bracket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = load_db()
    chat = get_chat(db, update.effective_chat.id)
    br = chat.get("bracket")
    if not br:
        await update.message.reply_text("Сетка ещё не создана. Используйте /draw.")
        return
    text = f"*{chat['name'] or 'Турнир'}*\n\n" + render_bracket_tree(br["rounds"])
    # Telegram has message length limits; send in chunks if needed
    MAX = 3500
    for i in range(0, len(text), MAX):
        await update.message.reply_text(text[i:i+MAX], parse_mode=ParseMode.MARKDOWN)


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = load_db()
    chat = get_chat(db, update.effective_chat.id)
    br = chat.get("bracket")
    if not br:
        await update.message.reply_text("Сетка ещё не создана. Используйте /draw.")
        return
    # Create CSV with Round, Match, Team A, Team B
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Round", "Match", "Team A", "Team B"])
    for r_idx, rnd in enumerate(br["rounds"], start=1):
        for m_idx, (a, b) in enumerate(rnd, start=1):
            writer.writerow([r_idx, m_idx, a, b])
    data = buf.getvalue().encode("utf-8")
    filename = f"{(chat['name'] or 'tournament').replace(' ', '_')}_bracket.csv"
    await update.message.reply_document(document=data, filename=filename, read_timeout=30)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = load_db()
    chat = get_chat(db, update.effective_chat.id)
    chat["teams"] = []
    chat["bracket"] = None
    save_db(db)
    await update.message.reply_text("Турнир очищен. Создайте новый /new и добавьте команды /add.")


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Please set BOT_TOKEN environment variable.")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("draw", cmd_draw))
    app.add_handler(CommandHandler("pairs", cmd_pairs))
    app.add_handler(CommandHandler("bracket", cmd_bracket))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("reset", cmd_reset))

    app.run_polling()


if __name__ == "__main__":
    main()
