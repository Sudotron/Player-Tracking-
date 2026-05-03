"""
bot.py — Main entry point for the CoC Player Tracker Bot.

Commands:
  /start               — Display the welcome message and instructions
  /trackplayer  <tag>  — Start tracking a player (runs in group to auto-set notifications)
  /untrackplayer       — Stop tracking; wipes all data
  /mystats             — Live snapshot of your tracked player
  /tracklist           — (owner only) All tracked players + who added them
  /botlog              — (owner only) Bot-wide stats
"""

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from functools import wraps

import coc
from dotenv import load_dotenv
from telegram import Update, BotCommand, BotCommandScopeDefault, BotCommandScopeChat, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
from telegram.ext import Application, CommandHandler, ContextTypes, ChatMemberHandler

import database as db
from tracker import setup_events, battle_log_poller
import notifier

# ─── Setup ───────────────────────────────────────────────────────────────────

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

BOT_TOKEN       = os.getenv("BOT_TOKEN", "")
COC_EMAIL       = os.getenv("COC_EMAIL", "")
COC_PASSWORD    = os.getenv("COC_PASSWORD", "")
OWNER_ID        = int(os.getenv("OWNER_ID", "0"))
ERROR_LOG_GROUP = int(os.getenv("ERROR_LOG_GROUP", "0"))
BOT_LOG_CHANNEL = int(os.getenv("BOT_LOG_CHANNEL", str(ERROR_LOG_GROUP)))

IST = timezone(timedelta(hours=5, minutes=30))

# ─── Access Decorators ───────────────────────────────────────────────────────

def owner_only(func):
    """Restrict a command handler to OWNER_ID."""
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != OWNER_ID:
            await update.message.reply_text("❌ This command is owner-only.")
            return
        return await func(update, ctx)
    return wrapper


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _coc(ctx: ContextTypes.DEFAULT_TYPE) -> coc.EventsClient:
    return ctx.bot_data["coc_client"]


def _fmt_tag(raw: str) -> str:
    tag = raw.strip().upper()
    return tag if tag.startswith("#") else "#" + tag


async def track_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log when the bot is added or removed from a group."""
    result = update.my_chat_member
    if not result:
        return
    
    status = result.new_chat_member.status
    chat = result.chat
    user = result.from_user
    
    if status in ["member", "administrator"]:
        db.register_group(chat.id, chat.title or "Group")
        msg = f"➕ <b>Bot Added to Group</b>\nGroup: {chat.title} (<code>{chat.id}</code>)\nAdded By: {user.first_name} (<code>{user.id}</code>)"
        await notifier.send_bot_log(context.bot, BOT_LOG_CHANNEL, msg)
    elif status in ["left", "kicked"]:
        msg = f"➖ <b>Bot Removed from Group</b>\nGroup: {chat.title} (<code>{chat.id}</code>)\nRemoved By: {user.first_name} (<code>{user.id}</code>)"
        await notifier.send_bot_log(context.bot, BOT_LOG_CHANNEL, msg)


# ─── Command Handlers ─────────────────────────────────────────────────────────

async def set_bot_commands(bot):
    user_commands = [
        BotCommand("start", "Display welcome message and instructions"),
        BotCommand("trackplayer", "Start tracking a player"),
        BotCommand("untrackplayer", "Stop tracking your current player"),
        BotCommand("mystats", "Live snapshot of your tracked player"),
        BotCommand("clanaudit", "Get analytics and profiles for a clan"),
    ]
    owner_commands = user_commands + [
        BotCommand("tracklist", "View all tracked players (Owner)"),
        BotCommand("botlog", "View bot statistics (Owner)"),
    ]
    try:
        await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
        await bot.set_my_commands(user_commands, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(user_commands, scope=BotCommandScopeAllGroupChats())
        if OWNER_ID:
            await bot.set_my_commands(owner_commands, scope=BotCommandScopeChat(chat_id=OWNER_ID))
    except Exception as e:
        logger.warning(f"Failed to set bot commands: {e}")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 <b>Welcome to the CoC Player Tracker Bot!</b>\n\n"
        "I can track Clash of Clans players and send real-time notifications about their activity to this chat.\n\n"
        "<b>Commands:</b>\n"
        "🔸 /trackplayer <code>#TAG</code> — Start tracking a player.\n"
        "🔸 /untrackplayer — Stop tracking your currently tracked player.\n"
        "🔸 /mystats — View live stats for your tracked player.\n"
    )

    if update.effective_user.id == OWNER_ID:
        msg += (
            "\n👑 <b>Owner Commands:</b>\n"
            "🔸 /tracklist — View all tracked players across all users.\n"
            "🔸 /botlog — View bot statistics.\n"
        )
    
    msg += (
        "\n📢 <b>Important:</b> To receive notifications in a group, simply add me to the group "
        "and send the /trackplayer command <b>inside the group</b>."
    )

    await update.message.reply_text(msg, parse_mode="HTML")

async def cmd_trackplayer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = ctx.args

    if not args:
        await update.message.reply_text(
            "❓ <b>Usage:</b> <code>/trackplayer #PLAYERTAG</code>",
            parse_mode="HTML",
        )
        return

    tag = _fmt_tag(args[0])

    # ── Duplicate-tag guard ───────────────────────────────────────────────────
    if db.is_tag_taken(tag, by_user_id=user.id):
        await update.message.reply_text(
            "❌ This player is already being tracked by <b>someone (anonymous)</b>.",
            parse_mode="HTML",
        )
        return

    # ── Validate tag against CoC API ──────────────────────────────────────────
    msg = await update.message.reply_text("⏳ Fetching player data…")
    try:
        player = await _coc(ctx).get_player(tag)
    except coc.NotFound:
        await msg.edit_text("❌ Player not found. Please check the tag.")
        return
    except Exception as e:
        await msg.edit_text(f"❌ API error: <code>{e}</code>", parse_mode="HTML")
        err = f"<b>/trackplayer Error</b>\nUser: {user.id}\nTag: <code>{tag}</code>\nError: {type(e).__name__}: {e}"
        await notifier.send_error(ctx.bot, ERROR_LOG_GROUP, err)
        return

    if not player:
        await msg.edit_text("❌ Player not found. Please check the tag.")
        return

    # ── Detect replacement ────────────────────────────────────────────────────
    old_row = db.get_tracked_player_by_user(user.id)
    replacing = (
        f"\n♻️ Replaced your previous tracking of <b>{old_row['player_name']}</b>."
        if old_row and old_row["player_tag"] != player.tag
        else ""
    )

    # ── Save to DB ────────────────────────────────────────────────────────────
    username = user.username or user.first_name
    success  = db.add_tracked_player(user.id, username, player.tag, player.name)
    if not success:
        await msg.edit_text(
            "❌ This player is already being tracked by <b>someone (anonymous)</b>.",
            parse_mode="HTML",
        )
        return

    # ── Notify Log Channel ────────────────────────────────────────────────────
    log_msg = (
        f"🎯 <b>New Tracking Started</b>\n"
        f"Player: {player.name} (<code>{player.tag}</code>)\n"
        f"Initiated By: {user.first_name} (<code>{user.id}</code>)"
    )
    await notifier.send_bot_log(ctx.bot, BOT_LOG_CHANNEL, log_msg)

    # ── Register with coc.py EventsClient ────────────────────────────────────
    _coc(ctx).add_player_updates(player.tag)   # v4: takes *args individually

    # ── Auto-set notification group if run inside a group chat ───────────
    chat = update.effective_chat
    group_note = ""
    if chat.type in ("group", "supergroup"):
        db.set_log_chat(user.id, chat.id, chat.title or "Group")
        db.register_group(chat.id, chat.title or "Group")
        group_note = f"\n\n📢 <b>Notifications will be sent to this group!</b>"
    else:
        group_note = (
            f"\n\n📢 <b>Next step:</b> Run <code>/trackplayer {player.tag}</code> "
            "inside your group to start receiving notifications there."
        )

    await msg.edit_text(
        f"✅ <b>Now tracking:</b> {player.name} ({player.tag})\n"
        f"🏰 TH {player.town_hall}  |  🏆 {player.trophies:,} trophies"
        f"{replacing}"
        f"{group_note}",
        parse_mode="HTML",
    )


async def cmd_untrackplayer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    existing = db.get_tracked_player_by_user(user.id)
    if not existing:
        await update.message.reply_text("❌ You are not currently tracking any player.")
        return

    tag  = existing["player_tag"]
    name = existing["player_name"]

    db.remove_tracked_player(user.id)

    # Remove from coc.py EventsClient (tag is exclusively owned by this user)
    coc_client = _coc(ctx)
    if hasattr(coc_client, "remove_player_updates"):
        coc_client.remove_player_updates([tag])

    await update.message.reply_text(
        f"🗑️ Stopped tracking <b>{name}</b> ({tag}).\n"
        f"All tracking data has been permanently deleted.",
        parse_mode="HTML",
    )



async def cmd_mystats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    existing = db.get_tracked_player_by_user(user.id)
    if not existing:
        await update.message.reply_text(
            "❌ You are not tracking any player.\n"
            "Use <code>/trackplayer #TAG</code> to start.",
            parse_mode="HTML",
        )
        return

    msg = await update.message.reply_text("⏳ Fetching live data…")
    try:
        player = await _coc(ctx).get_player(existing["player_tag"])
        await msg.edit_text(notifier.fmt_mystats(player), parse_mode="HTML",
                            disable_web_page_preview=True)
    except Exception as e:
        await msg.edit_text(f"❌ Failed to fetch player data.\n<code>{e}</code>",
                            parse_mode="HTML")
        err = f"<b>/mystats Error</b>\nUser: {user.id}\nTag: <code>{existing['player_tag']}</code>\nError: {type(e).__name__}: {e}"
        await notifier.send_error(ctx.bot, ERROR_LOG_GROUP, err)


@owner_only
async def cmd_tracklist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = db.get_all_tracked_info()

    if not rows:
        await update.message.reply_text("📋 No players are currently being tracked.")
        return

    lines = ["📋 <b>All Tracked Players</b>\n" + "━" * 25]
    for i, row in enumerate(rows, 1):
        uname    = f"@{row['username']}" if row["username"] else f"ID:{row['user_id']}"
        group    = row["log_chat_name"] or "❌ Not set"
        lines.append(
            f"{i}. <b>{row['player_name']}</b> (<code>{row['player_tag']}</code>)\n"
            f"   👤 By: {uname}\n"
            f"   📢 Group: {group}"
        )

    # Telegram max message length guard
    full = "\n".join(lines)
    if len(full) > 4000:
        full = full[:3990] + "\n…(truncated)"

    await update.message.reply_text(full, parse_mode="HTML")


@owner_only
async def cmd_botlog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    stats = db.get_bot_stats()
    now   = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")

    await update.message.reply_text(
        f"📊 <b>Bot Statistics</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total Users Tracking:  <b>{stats['users']}</b>\n"
        f"📢 Groups Using Bot:      <b>{stats['groups']}</b>\n"
        f"🎯 Players Being Tracked: <b>{stats['players']}</b>\n"
        f"🕐 {now}",
        parse_mode="HTML",
    )


async def cmd_clanaudit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🚧 <b>This command is currently in Beta.</b>\n"
        "Please check back later!",
        parse_mode="HTML"
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    db.init_db()
    logger.info("✅ Database initialised.")

    # ── Build Telegram app ────────────────────────────────────────────────────
    app = Application.builder().token(BOT_TOKEN).build()

    # ── Create and login coc.py EventsClient (v4 API) ─────────────────────────
    logger.info("🔐 Logging into CoC API via coc.py v4…")
    coc_client = coc.EventsClient(
        key_count=2,
        key_names="player_tracker_bot",
    )
    # In coc.py v4, login() is a coroutine — must be awaited
    await coc_client.login(COC_EMAIL, COC_PASSWORD)
    logger.info("✅ CoC API login successful.")

    # ── Store client + register events ────────────────────────────────────────
    app.bot_data["coc_client"] = coc_client
    setup_events(coc_client, app.bot)

    # ── Register all currently tracked tags with EventsClient ─────────────────
    tags = db.get_all_tracked_tags()
    if tags:
        coc_client.add_player_updates(*tags)   # v4: takes *args, not a list
        logger.info(f"📡 Registered {len(tags)} player tag(s) for live tracking.")

    # ── Command routing ───────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("trackplayer",   cmd_trackplayer))
    app.add_handler(CommandHandler("untrackplayer", cmd_untrackplayer))
    app.add_handler(CommandHandler("mystats",       cmd_mystats))
    app.add_handler(CommandHandler("tracklist",     cmd_tracklist))
    app.add_handler(CommandHandler("botlog",        cmd_botlog))
    app.add_handler(CommandHandler("clanaudit",     cmd_clanaudit))
    
    # ── Event routing ─────────────────────────────────────────────────────────
    app.add_handler(ChatMemberHandler(track_chats, ChatMemberHandler.MY_CHAT_MEMBER))

    logger.info("🤖 Starting bot…")

    async with app:
        await set_bot_commands(app.bot)
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        # Start battle log poller as background task
        poll_task = asyncio.create_task(battle_log_poller(coc_client, app.bot))

        # Notify owner that the bot is live
        try:
            await app.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    f"🟢 <b>Player Tracker Bot is Online</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"📡 Tracking {len(tags)} player(s)\n"
                    f"🕐 {datetime.now(IST).strftime('%I:%M %p IST')}"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass  # Startup notification is non-critical

        logger.info("🚀 Bot is running. Press Ctrl+C to stop.")

        # Block until interrupted
        stop = asyncio.Event()
        try:
            await stop.wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            logger.info("🔻 Shutting down…")
            poll_task.cancel()
            with suppress_exc():
                await coc_client.close()
            await app.updater.stop()
            await app.stop()
            logger.info("👋 Bot stopped cleanly.")


def suppress_exc():
    """Context manager that silences any exception (for cleanup only)."""
    import contextlib
    return contextlib.suppress(Exception)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
