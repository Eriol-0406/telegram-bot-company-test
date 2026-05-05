import asyncio
import os
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ApplicationBuilder, MessageHandler, filters, ContextTypes)

# Load environment variables
load_dotenv()
BOT_TOKEN     = os.getenv("BOT_TOKEN")
WEBSITE_URL   = os.getenv("WEBSITE_URL", "https://www.ccacc.io/")
X_URL         = os.getenv("X_URL", "https://x.com/ccacc_hub")
INSTAGRAM_URL = os.getenv("INSTAGRAM_URL", "https://www.instagram.com/ccacc_hub")
FORM_URL = os.getenv("FORM_URL", "https://forms.gle/RUa2FBhRDiJjHu199")
BANNER_URL = os.getenv("BANNER_URL", "https://raw.githubusercontent.com/Eriol-0406/telegram-bot-company/main/welcome_banner.jpeg")

# Logging setup
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# In-memory cooldown tracker { chat_id: datetime }
last_welcome_sent: dict[int, datetime] = {}
COOLDOWN_HOURS = 24


def is_cooldown_over(chat_id: int) -> bool:
    """Return True if 48hrs have passed since last welcome in this chat."""
    last = last_welcome_sent.get(chat_id)
    if last is None:
        return True
    return datetime.now() - last >= timedelta(hours=COOLDOWN_HOURS)


def update_cooldown(chat_id: int):
    """Reset the 48hr cooldown timer for this chat."""
    last_welcome_sent[chat_id] = datetime.now()


# Welcome handler
async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:

        # Ignore bots joining
        if member.is_bot:
            continue

        chat_id = update.effective_chat.id
        name    = member.first_name or member.username or "Builder"

        logger.info(f"New member joined: {name} (id={member.id})")

        # Case 1: No username set — always fires regardless of cooldown
        if not member.username:
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
            logger.info(f"{name} has no username, prompted to set one.")
        
            # Auto-delete after 30 seconds
            await asyncio.sleep(30)
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=sent.message_id,
            )
            logger.info(f"Deleted no-username prompt for {name}.")
            continue

        # Case 2: Has username but cooldown not over — skip silently
        if not is_cooldown_over(chat_id):
            remaining  = last_welcome_sent[chat_id] + timedelta(hours=COOLDOWN_HOURS) - datetime.now()
            hours_left = int(remaining.total_seconds() // 3600)
            mins_left  = int((remaining.total_seconds() % 3600) // 60)
            logger.info(
                f"Cooldown active for chat {chat_id}. "
                f"Next welcome in {hours_left}h {mins_left}m. "
                f"Skipping welcome for {name}."
            )
            continue

        # Case 3: Has username + cooldown is over — send full welcome
        welcome_text = (
            f"Hello, Web3 Fellows! 👋 Welcome to the official CCACC Chat!\n\n"
            f"CCACC is Malaysia's premier Web3 Innovation Hub 🚀, "
            f"dedicated to connecting the local blockchain ecosystem to the global stage "
            f"through capital, acceleration, and compliance."
        )

        if BANNER_URL:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=BANNER_URL,
                caption=welcome_text,
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=welcome_text,
            )

        # Inline buttons
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Website",   url=WEBSITE_URL)],
            [InlineKeyboardButton("𝕏  Twitter",   url=X_URL)],
            [InlineKeyboardButton("📸 Instagram", url=INSTAGRAM_URL)],
            [InlineKeyboardButton("📩 Apply for Internship", url=FORM_URL)],
        ])
        await context.bot.send_message(
            chat_id=chat_id,
            text="🔗 Quick Links:",
            reply_markup=keyboard,
        )

        # Update cooldown AFTER successfully sending welcome
        update_cooldown(chat_id)
        logger.info(f"Welcome sent to {name} (@{member.username}). Cooldown reset.")


# Error handler
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling update: {context.error}")


# Main
def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is missing! Check your .env or Railway variables.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member)
    )
    app.add_error_handler(error_handler)

    logger.info("Bot is running... Waiting for new members.")
    app.run_polling()


if __name__ == "__main__":
    main()
