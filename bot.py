"""
Telegram Delayed Join Request Bot
===================================
Accepts join requests to channels/groups after a configurable delay
(anywhere from 5 minutes to 4 weeks).

Setup:
  1. Create a bot via @BotFather and get your BOT_TOKEN
  2. Add the bot as an ADMIN to your channel/group
     (needs "Invite users via link" permission)
  3. Enable "Approve new members" on your channel/group
  4. Set BOT_TOKEN in .env or as environment variable
  5. Run:  python bot.py
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from telegram import (
    Bot,
    ChatJoinRequest,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "join_requests.db")

# ── Delay presets ──────────────────────────────────────────────────────────────
DELAY_OPTIONS = [
    ("5 minutes",   5 * 60),
    ("15 minutes",  15 * 60),
    ("30 minutes",  30 * 60),
    ("1 hour",      3600),
    ("3 hours",     3 * 3600),
    ("6 hours",     6 * 3600),
    ("12 hours",    12 * 3600),
    ("1 day",       86400),
    ("2 days",      2 * 86400),
    ("3 days",      3 * 86400),
    ("1 week",      7 * 86400),
    ("2 weeks",     14 * 86400),
    ("4 weeks",     28 * 86400),
]


# ── Database helpers ───────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS channel_settings (
                chat_id   INTEGER PRIMARY KEY,
                chat_name TEXT,
                delay_sec INTEGER NOT NULL DEFAULT 300
            );

            CREATE TABLE IF NOT EXISTS pending_requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                username    TEXT,
                first_name  TEXT,
                requested_at INTEGER NOT NULL,       -- unix timestamp
                approve_at   INTEGER NOT NULL,       -- unix timestamp
                status      TEXT NOT NULL DEFAULT 'pending',
                UNIQUE(chat_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_approve_at
                ON pending_requests(approve_at)
                WHERE status = 'pending';
        """)
    logger.info("Database ready ✔")


def get_setting(chat_id: int) -> Optional[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM channel_settings WHERE chat_id = ?", (chat_id,)
        ).fetchone()


def upsert_setting(chat_id: int, chat_name: str, delay_sec: int) -> None:
    with db_connect() as conn:
        conn.execute(
            """INSERT INTO channel_settings(chat_id, chat_name, delay_sec)
               VALUES(?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   chat_name = excluded.chat_name,
                   delay_sec = excluded.delay_sec""",
            (chat_id, chat_name, delay_sec),
        )


def queue_request(chat_id: int, user_id: int, username: str,
                  first_name: str, delay_sec: int) -> None:
    now = int(datetime.now(timezone.utc).timestamp())
    approve_at = now + delay_sec
    with db_connect() as conn:
        conn.execute(
            """INSERT INTO pending_requests
                   (chat_id, user_id, username, first_name, requested_at, approve_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(chat_id, user_id) DO UPDATE SET
                   requested_at = excluded.requested_at,
                   approve_at   = excluded.approve_at,
                   status       = 'pending'""",
            (chat_id, user_id, username, first_name, now, approve_at),
        )


def get_due_requests(now_ts: int) -> list[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            """SELECT * FROM pending_requests
               WHERE status = 'pending' AND approve_at <= ?""",
            (now_ts,),
        ).fetchall()


def mark_done(row_id: int, status: str) -> None:
    with db_connect() as conn:
        conn.execute(
            "UPDATE pending_requests SET status = ? WHERE id = ?",
            (status, row_id),
        )


def count_pending(chat_id: int) -> int:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM pending_requests "
            "WHERE chat_id = ? AND status = 'pending'",
            (chat_id,),
        ).fetchone()
        return row["n"] if row else 0


# ── Keyboards ──────────────────────────────────────────────────────────────────

def delay_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Build a grid of delay buttons."""
    buttons = []
    row: list[InlineKeyboardButton] = []
    for label, secs in DELAY_OPTIONS:
        row.append(
            InlineKeyboardButton(label, callback_data=f"delay:{chat_id}:{secs}")
        )
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


# ── Command handlers ───────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *Welcome to the Delayed Join Request Bot!*\n\n"
        "I can approve join requests to your channel or group after a "
        "configurable delay — anywhere from *5 minutes* to *4 weeks*.\n\n"
        "*How to set up:*\n"
        "1️⃣  Add me as an *admin* to your channel/group\n"
        "    -> give me *'Invite users via link'* permission\n"
        "2️⃣  Enable *'Approve new members'* on the chat\n"
        "3️⃣  Use /setchannel inside the channel/group (or pass the chat ID)\n"
        "4️⃣  Pick your delay with /setdelay\n\n"
        "Type /help for all commands.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📋 *Available commands*\n\n"
        "/start — Introduction\n"
        "/setchannel — Configure this chat for delayed approvals\n"
        "/setdelay — Change the approval delay for a configured chat\n"
        "/status — Show pending requests & current settings\n"
        "/help — This message\n\n"
        "*Admin note:* Run /setchannel *inside* the target channel/group, "
        "or forward a message from it to me in DM.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_setchannel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Register a chat and pick its delay."""
    chat = update.effective_chat
    user = update.effective_user

    # If called inside a group/channel, register that chat directly
    if chat.type in ("group", "supergroup", "channel"):
        chat_id = chat.id
        chat_name = chat.title or str(chat_id)
    else:
        # DM — try to get chat ID from args or ask
        if not ctx.args:
            await update.message.reply_text(
                "To configure a chat, either:\n"
                "• Run /setchannel *inside* the group/channel, OR\n"
                "• Run `/setchannel -100xxxxxxxxx` with the numeric chat ID",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        try:
            chat_id = int(ctx.args[0])
            info = await ctx.bot.get_chat(chat_id)
            chat_name = info.title or str(chat_id)
        except Exception:
            await update.message.reply_text(
                "❌ Could not find that chat. Make sure I'm already an admin there."
            )
            return

    # Check bot is admin
    try:
        member = await ctx.bot.get_chat_member(chat_id, ctx.bot.id)
        if member.status not in ("administrator", "creator"):
            raise ValueError("not admin")
    except Exception:
        await update.message.reply_text(
            f"❌ I'm not an admin in *{chat_name}*. "
            "Please add me as an admin with *'Invite users via link'* permission first.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Default delay = 1 hour; show picker
    current = get_setting(chat_id)
    current_delay = current["delay_sec"] if current else 3600
    upsert_setting(chat_id, chat_name, current_delay)

    await update.message.reply_text(
        f"✅ *{chat_name}* is configured!\n\n"
        f"Now choose the approval delay:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=delay_keyboard(chat_id),
    )


async def cmd_setdelay(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the delay picker for already-configured chats."""
    # Collect all settings
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM channel_settings").fetchall()

    if not rows:
        await update.message.reply_text(
            "No chats configured yet. Use /setchannel first."
        )
        return

    buttons = [
        [InlineKeyboardButton(
            f"{'📢' if r['chat_id'] < 0 else '💬'} {r['chat_name']}",
            callback_data=f"pick_chat:{r['chat_id']}"
        )]
        for r in rows
    ]
    await update.message.reply_text(
        "Which chat do you want to change the delay for?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show pending count and settings for all configured chats."""
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM channel_settings").fetchall()

    if not rows:
        await update.message.reply_text("No chats configured yet.")
        return

    lines = ["📊 *Current Status*\n"]
    for r in rows:
        pending = count_pending(r["chat_id"])
        delay_label = _secs_to_label(r["delay_sec"])
        lines.append(
            f"*{r['chat_name']}*\n"
            f"  • Delay: {delay_label}\n"
            f"  • Pending approvals: {pending}"
        )
    await update.message.reply_text(
        "\n\n".join(lines), parse_mode=ParseMode.MARKDOWN
    )


# ── Callback query handlers ────────────────────────────────────────────────────

async def cb_pick_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """After /setdelay, user chose a chat → show delay picker."""
    query = update.callback_query
    await query.answer()
    _, chat_id_str = query.data.split(":", 1)
    chat_id = int(chat_id_str)
    row = get_setting(chat_id)
    name = row["chat_name"] if row else str(chat_id)
    await query.edit_message_text(
        f"Choose the new approval delay for *{name}*:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=delay_keyboard(chat_id),
    )


async def cb_set_delay(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """User chose a delay from the keyboard → save it."""
    query = update.callback_query
    await query.answer()
    _, chat_id_str, secs_str = query.data.split(":")
    chat_id = int(chat_id_str)
    delay_sec = int(secs_str)

    row = get_setting(chat_id)
    if not row:
        await query.edit_message_text("❌ Chat not found. Run /setchannel first.")
        return

    upsert_setting(chat_id, row["chat_name"], delay_sec)
    label = _secs_to_label(delay_sec)
    pending = count_pending(chat_id)

    await query.edit_message_text(
        f"✅ *{row['chat_name']}*\n\n"
        f"Approval delay set to *{label}*.\n"
        f"New join requests will be approved after {label}.\n\n"
        f"Currently pending: *{pending}* request(s)",
        parse_mode=ParseMode.MARKDOWN,
    )
    logger.info("Delay for chat %s (%s) set to %s sec", chat_id, row["chat_name"], delay_sec)


# ── Join request handler ───────────────────────────────────────────────────────

async def handle_join_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    req: ChatJoinRequest = update.chat_join_request
    chat_id = req.chat.id
    user = req.from_user

    setting = get_setting(chat_id)
    if not setting:
        # Not configured — approve immediately (safe fallback)
        logger.warning(
            "Received join request for unconfigured chat %s — approving immediately",
            chat_id,
        )
        try:
            await req.approve()
        except Exception as e:
            logger.error("Could not approve immediately: %s", e)
        return

    delay_sec = setting["delay_sec"]
    queue_request(
        chat_id=chat_id,
        user_id=user.id,
        username=user.username or "",
        first_name=user.first_name or "",
        delay_sec=delay_sec,
    )
    label = _secs_to_label(delay_sec)
    logger.info(
        "Queued join request: user %s (%s) → chat %s, approve in %s",
        user.id, user.first_name, chat_id, label,
    )


# ── Background approval loop ───────────────────────────────────────────────────

async def approval_loop(bot: Bot) -> None:
    """Runs every 30 seconds — approves requests whose time has come."""
    logger.info("Approval loop started ✔")
    while True:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        due = get_due_requests(now_ts)

        for row in due:
            try:
                await bot.approve_chat_join_request(
                    chat_id=row["chat_id"],
                    user_id=row["user_id"],
                )
                mark_done(row["id"], "approved")
                logger.info(
                    "✅ Approved: user %s (%s) → chat %s",
                    row["user_id"], row["first_name"], row["chat_id"],
                )
            except Exception as e:
                err = str(e)
                if "USER_ALREADY_PARTICIPANT" in err or "HIDE_REQUESTER_MISSING" in err:
                    mark_done(row["id"], "already_joined")
                    logger.info("ℹ️  Already joined: user %s", row["user_id"])
                elif "USER_CHANNELS_TOO_MUCH" in err:
                    mark_done(row["id"], "failed_too_many_channels")
                    logger.warning("User %s in too many channels", row["user_id"])
                else:
                    logger.error(
                        "Failed to approve user %s: %s", row["user_id"], e
                    )

        await asyncio.sleep(30)


# ── Utility ────────────────────────────────────────────────────────────────────

def _secs_to_label(secs: int) -> str:
    for label, s in DELAY_OPTIONS:
        if s == secs:
            return label
    # Fallback for custom values
    if secs < 3600:
        return f"{secs // 60} minute(s)"
    if secs < 86400:
        return f"{secs // 3600} hour(s)"
    return f"{secs // 86400} day(s)"


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set! Add it to .env or as an environment variable.")
        raise SystemExit(1)

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("setchannel", cmd_setchannel))
    app.add_handler(CommandHandler("setdelay", cmd_setdelay))
    app.add_handler(CommandHandler("status", cmd_status))

    # Callbacks
    app.add_handler(CallbackQueryHandler(cb_pick_chat, pattern=r"^pick_chat:"))
    app.add_handler(CallbackQueryHandler(cb_set_delay, pattern=r"^delay:"))

    # Join requests
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    # Unknown commands
    app.add_handler(
        MessageHandler(filters.COMMAND, lambda u, c: u.message.reply_text(
            "Unknown command. Try /help"
        ))
    )

    async def post_init(application: Application) -> None:
        asyncio.create_task(approval_loop(application.bot))

    app.post_init = post_init

    logger.info("Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
