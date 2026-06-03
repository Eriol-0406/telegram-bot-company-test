import asyncio
import json
import os
import logging
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes,
)

# Load environment variables
load_dotenv()
BOT_TOKEN     = os.getenv("BOT_TOKEN")
WEBSITE_URL   = os.getenv("WEBSITE_URL", "https://www.ccacc.io/")
X_URL         = os.getenv("X_URL", "https://x.com/ccacc_hub")
INSTAGRAM_URL = os.getenv("INSTAGRAM_URL", "https://www.instagram.com/ccacc_hub")
FORM_URL      = os.getenv("FORM_URL", "https://forms.gle/RUa2FBhRDiJjHu199")
BANNER_URL    = os.getenv("BANNER_URL", "https://raw.githubusercontent.com/Eriol-0406/telegram-bot-company/main/welcome_banner.jpeg")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")  # Auto-detected if not set

# Cooldown: set COOLDOWN_MINUTES env var for testing (default 1440 = 24 hours)
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "1440"))

# Logging setup
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Persistent data file ──────────────────────────────────────────────
DATA_FILE = Path(os.getenv("DATA_FILE", "bot_data.json"))


def _load_data() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            logger.warning("Corrupt data file, starting fresh.")
    return {"new_users": [], "left_users": [], "activity": {}, "group_chat_id": None}


def _save_data(data: dict):
    DATA_FILE.write_text(json.dumps(data, default=str, indent=2))


data = _load_data()

# ── Welcome cooldown ──────────────────────────────────────────────────
last_welcome_sent: dict[int, datetime] = {}


def is_cooldown_over(chat_id: int) -> bool:
    last = last_welcome_sent.get(chat_id)
    if last is None:
        return True
    return datetime.now() - last >= timedelta(minutes=COOLDOWN_MINUTES)


def update_cooldown(chat_id: int):
    last_welcome_sent[chat_id] = datetime.now()


# ── Group ID resolution ──────────────────────────────────────────────
def _get_tracked_group_id() -> int | None:
    """Get the group chat ID from env, or auto-detected from data."""
    if GROUP_CHAT_ID:
        return int(GROUP_CHAT_ID)
    if data.get("group_chat_id"):
        return int(data["group_chat_id"])
    return None


def _auto_detect_group(chat_id: int):
    """Auto-detect and save the group chat ID on first group interaction."""
    if GROUP_CHAT_ID:
        return  # Env var takes priority
    if data.get("group_chat_id") is None:
        data["group_chat_id"] = chat_id
        _save_data(data)
        logger.info(f"Auto-detected group chat ID: {chat_id}")


def _is_tracked_group(chat_id: int) -> bool:
    group_id = _get_tracked_group_id()
    if group_id is None:
        return True  # Track all groups until one is detected
    return chat_id == group_id


# ── Tracking helpers ──────────────────────────────────────────────────
def log_new_user(chat_id: int, member):
    data["new_users"].append({
        "chat_id": chat_id,
        "user_id": member.id,
        "first_name": member.first_name or "",
        "username": member.username or "",
        "joined_at": datetime.now().isoformat(),
    })
    _save_data(data)


def log_left_user(chat_id: int, member):
    data["left_users"].append({
        "chat_id": chat_id,
        "user_id": member.id,
        "first_name": member.first_name or "",
        "username": member.username or "",
        "left_at": datetime.now().isoformat(),
    })
    _save_data(data)


def track_activity(chat_id: int, user):
    uid = str(user.id)
    if uid not in data["activity"]:
        data["activity"][uid] = {
            "user_id": user.id,
            "first_name": user.first_name or "",
            "username": user.username or "",
            "message_count": 0,
            "first_seen": datetime.now().isoformat(),
            "last_seen": datetime.now().isoformat(),
            "daily": {},
        }
    entry = data["activity"][uid]
    entry["first_name"] = user.first_name or entry["first_name"]
    entry["username"] = user.username or entry["username"]
    entry["message_count"] += 1
    entry["last_seen"] = datetime.now().isoformat()

    today = datetime.now().strftime("%Y-%m-%d")
    entry["daily"][today] = entry["daily"].get(today, 0) + 1

    # Keep only last 90 days of daily data
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    entry["daily"] = {k: v for k, v in entry["daily"].items() if k >= cutoff}

    _save_data(data)


# ── Admin check ───────────────────────────────────────────────────────
async def is_admin(bot, user_id: int, group_chat_id: int) -> bool:
    # Try the stored ID first, then with -100 prefix if it's positive
    ids_to_try = [group_chat_id]
    if group_chat_id > 0:
        ids_to_try.append(int(f"-100{group_chat_id}"))

    for gid in ids_to_try:
        try:
            member = await bot.get_chat_member(gid, user_id)
            logger.info(f"Admin check: user {user_id} status='{member.status}' in chat {gid}")
            if member.status in ("administrator", "creator"):
                # Fix the stored group_chat_id if the -100 prefix was needed
                if gid != group_chat_id and data.get("group_chat_id") == group_chat_id:
                    data["group_chat_id"] = gid
                    _save_data(data)
                    logger.info(f"Fixed stored group_chat_id to {gid}")
                return True
            return False
        except Exception as e:
            logger.warning(f"Admin check with chat_id {gid} failed: {e}")
            continue

    logger.error(f"Admin check exhausted all IDs for user {user_id}, group {group_chat_id}")
    return False


async def _require_private_admin(update, context) -> int | None:
    """Ensure command is in private chat and user is group admin.
    Returns the group_chat_id or None if checks fail."""
    if update.effective_chat.type != "private":
        # Silently delete command in group, redirect to DM
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
        except Exception:
            pass
        try:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text="Please use admin commands here in our private chat only.",
            )
        except Exception:
            pass
        return None

    group_id = _get_tracked_group_id()
    if group_id is None:
        await update.message.reply_text(
            "No group detected yet. Either:\n"
            "1. Set GROUP_CHAT_ID in Railway env vars, or\n"
            "2. Add the bot to your group first — it auto-detects the group ID.\n\n"
            "Tip: Add @userinfobot to your group to find the chat ID."
        )
        return None

    user_id = update.effective_user.id
    if not await is_admin(context.bot, user_id, group_id):
        # Reload in case it was corrected
        corrected_id = _get_tracked_group_id()
        await update.message.reply_text(
            f"Admin check failed.\n\n"
            f"Your user ID: {user_id}\n"
            f"Group ID tried: {group_id}\n"
            f"(also tried: -100{group_id})\n\n"
            f"Make sure:\n"
            f"1. The bot is an admin in the group\n"
            f"2. You are an admin/owner of the group\n\n"
            f"Check Railway logs for the exact Telegram API error."
        )
        return None

    return group_id


async def _delete_message_later(bot, chat_id: int, message_id: int, delay: int):
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(f"Failed to delete message {message_id}: {e}")


# ── Group handlers (silent tracking) ─────────────────────────────────

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue

        chat_id = update.effective_chat.id
        name = member.first_name or member.username or "Builder"

        logger.info(f"New member joined: {name} (id={member.id}) in chat {chat_id}")

        # Auto-detect group and track
        _auto_detect_group(chat_id)
        if _is_tracked_group(chat_id):
            log_new_user(chat_id, member)

        # Case 1: No username — prompt (always, ignore cooldown)
        if not member.username:
            try:
                sent = await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"👋 Welcome, {name}!\n\n"
                        f"To fully join our community, you'll need a Telegram username 🫶 ~~\n\n"
                        f"💡 Tip: Setting a Telegram username helps other members tag and find you easily!\n\n"
                        f"📱 How to set one up:\n"
                        f"Settings → Edit Profile → Username\n\n"
                        f"Once done, you're all set to participate. See you inside! 🚀🫶\n\n"
                        f"_(This message will be removed in 30 seconds)_"
                    ),
                    parse_mode="Markdown",
                )
                asyncio.create_task(
                    _delete_message_later(context.bot, chat_id, sent.message_id, 30)
                )
            except Exception as e:
                logger.error(f"Failed to send no-username prompt for {name}: {e}")
            continue

        # Case 2: Cooldown active — skip
        if not is_cooldown_over(chat_id):
            remaining = last_welcome_sent[chat_id] + timedelta(minutes=COOLDOWN_MINUTES) - datetime.now()
            mins_left = int(remaining.total_seconds() // 60)
            logger.info(f"Cooldown active ({mins_left}m left), skipping welcome for {name}.")
            continue

        # Case 3: Full welcome
        try:
            welcome_text = (
                f"Hello, Web3 Fellows! 👋 Welcome to the official CCACC Chat!\n\n"
                f"CCACC is Malaysia's premier Web3 Innovation Hub 🚀, "
                f"dedicated to connecting the local blockchain ecosystem to the global stage "
                f"through capital, acceleration, and compliance."
            )
            if BANNER_URL:
                await context.bot.send_photo(
                    chat_id=chat_id, photo=BANNER_URL, caption=welcome_text,
                )
            else:
                await context.bot.send_message(chat_id=chat_id, text=welcome_text)

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Website",   url=WEBSITE_URL)],
                [InlineKeyboardButton("𝕏  Twitter",   url=X_URL)],
                [InlineKeyboardButton("📸 Instagram", url=INSTAGRAM_URL)],
                [InlineKeyboardButton("📩 Apply for Internship", url=FORM_URL)],
            ])
            await context.bot.send_message(
                chat_id=chat_id, text="🔗 Quick Links:", reply_markup=keyboard,
            )
            update_cooldown(chat_id)
            logger.info(f"Welcome sent to {name} (@{member.username}). Cooldown reset ({COOLDOWN_MINUTES}m).")
        except Exception as e:
            logger.error(f"Failed to send welcome for {name}: {e}")


async def track_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    member = update.message.left_chat_member
    if member and not member.is_bot:
        chat_id = update.effective_chat.id
        _auto_detect_group(chat_id)
        if _is_tracked_group(chat_id):
            log_left_user(chat_id, member)
            logger.info(f"Member left: {member.first_name} (id={member.id})")


async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Silently track every message for activity stats."""
    if update.effective_user and update.effective_user.is_bot:
        return
    chat_id = update.effective_chat.id
    if update.effective_chat.type in ("group", "supergroup"):
        _auto_detect_group(chat_id)
        if _is_tracked_group(chat_id):
            track_activity(chat_id, update.effective_user)


# ── Private DM admin commands ────────────────────────────────────────

async def cmd_newusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show new users. Usage: /newusers [days]"""
    group_id = await _require_private_admin(update, context)
    if group_id is None:
        return

    days = None
    for arg in (context.args or []):
        try:
            val = int(arg)
            if 0 < val < 10000:
                days = val
        except ValueError:
            pass

    users = [u for u in data["new_users"] if str(u["chat_id"]) == str(group_id)]

    if days is not None:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        users = [u for u in users if u["joined_at"] >= cutoff]
        period = f"last {days} day(s)"
    else:
        period = "since tracking started"

    count = len(users)
    if count == 0:
        await update.message.reply_text(f"No new users {period}.")
        return

    lines = [f"*New Users ({period}):* {count}\n"]
    for u in users[-25:]:
        display = f"@{u['username']}" if u["username"] else u["first_name"]
        joined = u["joined_at"][:16].replace("T", " ")
        lines.append(f"  {display} — {joined}")
    if count > 25:
        lines.append(f"\n_(showing last 25 of {count})_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_left(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show users who left. Usage: /left [days]"""
    group_id = await _require_private_admin(update, context)
    if group_id is None:
        return

    days = None
    for arg in (context.args or []):
        try:
            val = int(arg)
            if 0 < val < 10000:
                days = val
        except ValueError:
            pass

    users = [u for u in data["left_users"] if str(u["chat_id"]) == str(group_id)]

    if days is not None:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        users = [u for u in users if u["left_at"] >= cutoff]
        period = f"last {days} day(s)"
    else:
        period = "since tracking started"

    count = len(users)
    if count == 0:
        await update.message.reply_text(f"No users left {period}.")
        return

    lines = [f"*Users Left ({period}):* {count}\n"]
    for u in users[-25:]:
        display = f"@{u['username']}" if u["username"] else u["first_name"]
        left = u["left_at"][:16].replace("T", " ")
        lines.append(f"  {display} — {left}")
    if count > 25:
        lines.append(f"\n_(showing last 25 of {count})_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show most active users. Usage: /active [days]"""
    group_id = await _require_private_admin(update, context)
    if group_id is None:
        return

    days = 7
    for arg in (context.args or []):
        try:
            val = int(arg)
            if 0 < val < 10000:
                days = val
        except ValueError:
            pass

    cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    ranked = []
    for uid, info in data["activity"].items():
        recent_msgs = sum(
            count for date, count in info["daily"].items() if date >= cutoff_date
        )
        if recent_msgs > 0:
            ranked.append((recent_msgs, info))

    ranked.sort(key=lambda x: x[0], reverse=True)

    if not ranked:
        await update.message.reply_text(f"No activity in the last {days} day(s).")
        return

    lines = [f"*Most Active Users (last {days} days):*\n"]
    for i, (count, info) in enumerate(ranked[:20], 1):
        display = f"@{info['username']}" if info["username"] else info["first_name"]
        last_seen = info["last_seen"][:16].replace("T", " ")
        lines.append(f"  {i}. {display} — {count} msgs (last: {last_seen})")

    total_msgs = sum(c for c, _ in ranked)
    lines.append(f"\n_Total: {total_msgs} messages from {len(ranked)} users_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show users inactive for N days. Usage: /inactive [days] (default 7)"""
    group_id = await _require_private_admin(update, context)
    if group_id is None:
        return

    days = 7
    for arg in (context.args or []):
        try:
            val = int(arg)
            if 0 < val < 10000:
                days = val
        except ValueError:
            pass

    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    inactive = []
    for uid, info in data["activity"].items():
        if info["last_seen"] < cutoff:
            inactive.append(info)

    inactive.sort(key=lambda x: x["last_seen"])

    if not inactive:
        await update.message.reply_text(f"No inactive users (>{days} days).")
        return

    lines = [f"*Inactive Users (no messages in {days}+ days):* {len(inactive)}\n"]
    for info in inactive[:25]:
        display = f"@{info['username']}" if info["username"] else info["first_name"]
        last = info["last_seen"][:16].replace("T", " ")
        lines.append(f"  {display} — last seen {last} ({info['message_count']} total msgs)")
    if len(inactive) > 25:
        lines.append(f"\n_(showing 25 of {len(inactive)})_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Overall group summary. Usage: /stats"""
    group_id = await _require_private_admin(update, context)
    if group_id is None:
        return

    gid = str(group_id)
    total_joined = len([u for u in data["new_users"] if str(u["chat_id"]) == gid])
    total_left = len([u for u in data["left_users"] if str(u["chat_id"]) == gid])
    total_tracked = len(data["activity"])

    today = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    joined_today = len([u for u in data["new_users"]
                        if str(u["chat_id"]) == gid and u["joined_at"][:10] == today])
    joined_week = len([u for u in data["new_users"]
                       if str(u["chat_id"]) == gid and u["joined_at"][:10] >= week_ago])
    joined_month = len([u for u in data["new_users"]
                        if str(u["chat_id"]) == gid and u["joined_at"][:10] >= month_ago])

    left_week = len([u for u in data["left_users"]
                     if str(u["chat_id"]) == gid and u["left_at"][:10] >= week_ago])

    msgs_today = sum(
        info["daily"].get(today, 0) for info in data["activity"].values()
    )
    msgs_week = sum(
        sum(c for d, c in info["daily"].items() if d >= week_ago)
        for info in data["activity"].values()
    )
    active_today = sum(
        1 for info in data["activity"].values() if info["daily"].get(today, 0) > 0
    )

    lines = [
        "*Group Analytics Dashboard*\n",
        "*Membership:*",
        f"  Joined (total): {total_joined}",
        f"  Joined today: {joined_today}",
        f"  Joined (7d): {joined_week}",
        f"  Joined (30d): {joined_month}",
        f"  Left (7d): {left_week}",
        f"  Net growth (7d): {joined_week - left_week}",
        "",
        "*Activity:*",
        f"  Tracked users: {total_tracked}",
        f"  Messages today: {msgs_today}",
        f"  Messages (7d): {msgs_week}",
        f"  Active today: {active_today} users",
        "",
        f"_Cooldown: {COOLDOWN_MINUTES}m | Group: {group_id}_",
    ]

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reveal the chat ID. Works in groups — auto-deletes after 10s."""
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    sent = await update.message.reply_text(
        f"Chat ID: `{chat_id}`\nType: {chat_type}\n\n"
        f"_(this message will self-destruct in 10 seconds)_",
        parse_mode="Markdown",
    )
    # Delete both the command and the reply after 10 seconds
    asyncio.create_task(_delete_message_later(context.bot, chat_id, update.message.message_id, 10))
    asyncio.create_task(_delete_message_later(context.bot, chat_id, sent.message_id, 10))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available admin commands (private DM only)."""
    if update.effective_chat.type != "private":
        return

    await update.message.reply_text(
        "*Admin Commands* (DM only)\n\n"
        "/stats — Group dashboard overview\n"
        "/newusers [days] — New user joins\n"
        "/left [days] — Users who left\n"
        "/active [days] — Most active users (default 7d)\n"
        "/inactive [days] — Gone quiet users (default 7d)\n"
        "/adminhelp — This help message\n\n"
        "_All commands verify you're a group admin._\n"
        "_Bot must be admin in the group for this to work._",
        parse_mode="Markdown",
    )


# ── Error handler ────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling update: {context.error}")


# ── Main ─────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is missing! Check your .env or Railway variables.")

    logger.info(f"Cooldown set to {COOLDOWN_MINUTES} minutes.")
    if GROUP_CHAT_ID:
        logger.info(f"Tracking group: {GROUP_CHAT_ID}")
    else:
        logger.info("No GROUP_CHAT_ID set — will auto-detect from first group interaction.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Group handlers
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member
    ))
    app.add_handler(MessageHandler(
        filters.StatusUpdate.LEFT_CHAT_MEMBER, track_left_member
    ))
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
        track_message,
    ), group=1)

    # Private DM admin commands
    app.add_handler(CommandHandler("newusers", cmd_newusers))
    app.add_handler(CommandHandler("left", cmd_left))
    app.add_handler(CommandHandler("active", cmd_active))
    app.add_handler(CommandHandler("inactive", cmd_inactive))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("adminhelp", cmd_help))

    app.add_error_handler(error_handler)

    logger.info("Bot is running... Waiting for new members.")
    app.run_polling()


if __name__ == "__main__":
    main()
