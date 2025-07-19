import asyncio
import logging
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import requests
import json
import time
from flask import Flask, request

# Flask app for webhook
app = Flask(__name__)

# Logging setup
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Read environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "temp-mail-bot.onrender.com")  # Default fallback

# Check if environment variables are set
if not all([BOT_TOKEN, ADMIN_ID, CHANNEL_USERNAME]):
    raise ValueError("Missing required environment variables: BOT_TOKEN, ADMIN_ID, or CHANNEL_USERNAME")

# Store user data (chat_id: {email_info, verified})
users = {}

# mail.tm API endpoints
MAIL_TM_BASE_URL = "https://api.mail.tm"
DOMAINS_ENDPOINT = f"{MAIL_TM_BASE_URL}/domains"
ACCOUNTS_ENDPOINT = f"{MAIL_TM_BASE_URL}/accounts"
MESSAGES_ENDPOINT = f"{MAIL_TM_BASE_URL}/messages"
TOKEN_ENDPOINT = f"{MAIL_TM_BASE_URL}/token"

# Telegram application (webhook mode only)
application = Application.builder().token(BOT_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command"""
    user_id = update.effective_user.id
    await update.message.reply_text(
        f"Welcome to Temp Mail Bot! 📧\n"
        f"Please join our channel {CHANNEL_USERNAME} to use the bot.\n"
        f"After joining, use /verify to activate the bot.\n"
        f"Commands: /new, /check, /delete"
    )
    if user_id not in users:
        users[user_id] = {"email": None, "token": None, "verified": False}

async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Verify if user has joined the channel"""
    user_id = update.effective_user.id
    try:
        chat_member = await context.bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        if chat_member.status in ['member', 'administrator', 'creator']:
            users[user_id]["verified"] = True
            await update.message.reply_text(
                "Verification successful! 🎉\n"
                "You can now use: /new, /check, /delete"
            )
        else:
            await update.message.reply_text(
                f"Please join {CHANNEL_USERNAME} first, then use /verify again."
            )
    except Exception as e:
        logger.error(f"Error verifying user {user_id}: {e}")
        await update.message.reply_text(
            f"Error: Could not verify. Ensure you joined {CHANNEL_USERNAME} and try again."
        )

async def check_verification(user_id: int, context: ContextTypes.DEFAULT_TYPE, update: Update) -> bool:
    """Check if user is verified"""
    if user_id not in users or not users[user_id]["verified"]:
        await update.message.reply_text(
            f"Please join {CHANNEL_USERNAME} and use /verify to activate the bot."
        )
        return False
    return True

async def new_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a new temporary email"""
    user_id = update.effective_user.id
    if not await check_verification(user_id, context, update):
        return

    try:
        response = requests.get(DOMAINS_ENDPOINT)
        domains = response.json()['hydra:member']
        if not domains:
            await update.message.reply_text("Error: No domains available.")
            return
        domain = domains[0]['domain']

        email = f"user{int(time.time())}@{domain}"
        password = f"pass{int(time.time())}"

        payload = {"address": email, "password": password}
        response = requests.post(ACCOUNTS_ENDPOINT, json=payload)
        if response.status_code != 201:
            await update.message.reply_text("Error: Could not create email.")
            return
        account = response.json()

        response = requests.post(TOKEN_ENDPOINT, json=payload)
        if response.status_code != 200:
            await update.message.reply_text("Error: Could not authenticate email.")
            return
        token = response.json()['token']

        users[user_id]["email"] = email
        users[user_id]["token"] = token
        await update.message.reply_text(f"Your new temporary email is: {email}")
    except Exception as e:
        logger.error(f"Error creating email: {e}")
        await update.message.reply_text("Error: Something went wrong. Try again.")

async def check_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check inbox for temporary email"""
    user_id = update.effective_user.id
    if not await check_verification(user_id, context, update):
        return

    if not users[user_id]["email"]:
        await update.message.reply_text("No email found. Use /new to create one.")
        return

    try:
        headers = {"Authorization": f"Bearer {users[user_id]['token']}"}
        response = requests.get(MESSAGES_ENDPOINT, headers=headers)
        messages = response.json()['hydra:member']

        if not messages:
            await update.message.reply_text("Your inbox is empty.")
            return

        reply = "Inbox:\n"
        for msg in messages[:5]:
            reply += f"From: {msg['from']['address']}\nSubject: {msg['subject']}\n\n"
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error(f"Error checking inbox: {e}")
        await update.message.reply_text("Error: Could not check inbox.")

async def delete_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete temporary email"""
    user_id = update.effective_user.id
    if not await check_verification(user_id, context, update):
        return

    if not users[user_id]["email"]:
        await update.message.reply_text("No email found. Use /new to create one.")
        return

    try:
        headers = {"Authorization": f"Bearer {users[user_id]['token']}"}
        response = requests.delete(f"{ACCOUNTS_ENDPOINT}/{users[user_id]['email']}", headers=headers)
        if response.status_code == 204:
            await update.message.reply_text("Email deleted successfully.")
            users[user_id]["email"] = None
            users[user_id]["token"] = None
        else:
            await update.message.reply_text("Error: Could not delete email.")
    except Exception as e:
        logger.error(f"Error deleting email: {e}")
        await update.message.reply_text("Error: Something went wrong.")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message to all users (admin only)"""
    user_id = update.effective_user.id
    if str(user_id) != ADMIN_ID:
        await update.message.reply_text("You are not authorized to use this command.")
        return

    if not context.args:
        await update.message.reply_text("Please provide a message to broadcast. Usage: /broadcast <message>")
        return

    message = " ".join(context.args)
    sent_count = 0
    for user_id in users:
        try:
            await context.bot.send_message(chat_id=user_id, text=f"Admin Broadcast: {message}")
            sent_count += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Error sending broadcast to {user_id}: {e}")

    await update.message.reply_text(f"Broadcast sent to {sent_count} users.")

@app.route('/webhook', methods=['POST'])
async def webhook():
    """Handle Telegram webhook requests"""
    try:
        update = Update.de_json(request.get_json(), application.bot)
        if update:
            await application.process_update(update)
        return '', 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return '', 500

async def set_webhook():
    """Set Telegram webhook"""
    webhook_url = f"https://{RENDER_EXTERNAL_HOSTNAME}/webhook"
    try:
        await application.bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook set to {webhook_url}")
    except Exception as e:
        logger.error(f"Error setting webhook: {e}")

if __name__ == "__main__":
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("verify", verify))
    application.add_handler(CommandHandler("new", new_email))
    application.add_handler(CommandHandler("check", check_inbox))
    application.add_handler(CommandHandler("delete", delete_email))
    application.add_handler(CommandHandler("broadcast", broadcast))

    # Set webhook and run Flask app
    asyncio.run(set_webhook())
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 8443)))
