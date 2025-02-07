import os
import sys
import requests
import time
from datetime import datetime
import pytz
import threading
import re
import logging
import asyncio
from flask import Flask, request, Response

# -------------------- Logging Setup --------------------
# Log to standard output so Vercel can capture the logs.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

# -------------------- Environment Variables --------------------
# These will be set in Vercel’s environment.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # e.g. "https://your-project.vercel.app"

if not TELEGRAM_BOT_TOKEN:
    logging.error("TELEGRAM_BOT_TOKEN is not set in environment variables.")
if not RAPIDAPI_KEY:
    logging.error("RAPIDAPI_KEY is not set in environment variables.")
if not WEBHOOK_URL:
    logging.error("WEBHOOK_URL is not set in environment variables.")

# -------------------- Global Variables --------------------
# This dictionary will track stop events for monitoring threads.
monitor_flags = {}

# -------------------- Telegram & Bot Libraries --------------------
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# -------------------- Tweet Scraper Functions --------------------
def get_tweet_text(tweet_data):
    """Extract full_text from the complex tweet JSON structure."""
    try:
        return tweet_data['result']['timeline']['instructions'][1]['entries'][0]\
            ['content']['itemContent']['tweet_results']['result']['legacy']['full_text']
    except (KeyError, IndexError):
        return None

def get_tweet_id(tweet_data):
    """Extract tweet ID from the complex tweet JSON structure."""
    try:
        return tweet_data['result']['timeline']['instructions'][1]['entries'][0]\
            ['content']['itemContent']['tweet_results']['result']['legacy']['id_str']
    except (KeyError, IndexError):
        return None

def get_ist_time():
    """Get current time in IST."""
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(pytz.utc).astimezone(ist)
    return now.strftime("%Y-%m-%d %H:%M:%S IST")

def get_user_tweets(user_id, api_key):
    """Fetch tweets for a specific user using RapidAPI."""
    url = f"https://twitter241.p.rapidapi.com/user-tweets?user={user_id}&count=20"
    headers = {
        "x-rapidapi-host": "twitter241.p.rapidapi.com",
        "x-rapidapi-key": api_key
    }
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching tweets for {user_id}: {e}")
        return None

def send_telegram_message(message, target_chat_id):
    """Send a message to a Telegram chat using the Bot API."""
    telegram_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {'chat_id': target_chat_id, 'text': message}
    try:
        response = requests.post(telegram_url, data=payload)
        if response.status_code != 200:
            logging.error(f"Failed to send message: {response.text}")
    except Exception as e:
        logging.error(f"Error sending message: {e}")

def monitor_tweets(twitter_user_id, rapidapi_key, target_chat_id, stop_event, keywords):
    """
    Poll for tweets for the given Twitter user and send notifications via Telegram.
    Runs in a separate thread until stop_event is set.
    """
    latest_tweet_id = None
    logging.info(f"Started monitoring tweets for {twitter_user_id} in chat {target_chat_id}.")
    while not stop_event.is_set():
        try:
            tweets_data = get_user_tweets(twitter_user_id, rapidapi_key)
            if tweets_data:
                tweet_text = get_tweet_text(tweets_data)
                tweet_id = get_tweet_id(tweets_data)
                if tweet_text and tweet_id and (latest_tweet_id is None or tweet_id != latest_tweet_id):
                    # If keywords are provided, filter tweets
                    if keywords:
                        if not any(keyword.lower() in tweet_text.lower() for keyword in keywords):
                            logging.info(f"Tweet {tweet_id} skipped due to keyword filter.")
                            time.sleep(5)
                            continue
                    latest_tweet_id = tweet_id
                    timestamp = get_ist_time()
                    retweet_flag = tweet_text.startswith("RT ")
                    retweet_text = "Retweet" if retweet_flag else "Original Tweet"
                    hashtags = re.findall(r"#\w+", tweet_text)
                    hashtags_text = ", ".join(hashtags) if hashtags else "None"
                    tweet_link = f"https://twitter.com/i/web/status/{tweet_id}"
                    msg = (
                        f"[{timestamp}] New tweet detected!\n"
                        f"Twitter User: {twitter_user_id}\n"
                        f"Tweet ID: {tweet_id}\n"
                        f"Type: {retweet_text}\n"
                        f"Hashtags: {hashtags_text}\n"
                        f"Content: {tweet_text}\n"
                        f"Link: {tweet_link}\n"
                        + "-"*50
                    )
                    send_telegram_message(msg, target_chat_id)
            time.sleep(5)
        except Exception as e:
            err_msg = f"An error occurred in tweet monitor for {twitter_user_id}: {e}"
            logging.error(err_msg)
            send_telegram_message(err_msg, target_chat_id)
            time.sleep(5)
    logging.info(f"Stopped monitoring tweets for {twitter_user_id} in chat {target_chat_id}.")

# -------------------- Telegram Bot Handlers --------------------
# Conversation states for /start and /stop
TWITTER_ID, KEYWORDS, STOP_STATE = range(3)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Welcome! Please send me the Twitter user ID you want to monitor.")
    return TWITTER_ID

async def receive_twitter_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    twitter_user_id = update.message.text.strip()
    context.user_data['twitter_user_id'] = twitter_user_id
    await update.message.reply_text("Optional: Enter filter keywords separated by commas, or type 'none' to monitor all tweets.")
    return KEYWORDS

async def receive_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text.lower() == 'none':
        keywords = []
    else:
        keywords = [word.strip() for word in text.split(",") if word.strip()]
    context.user_data['keywords'] = keywords
    target_chat_id = update.message.chat.id
    context.user_data['target_chat_id'] = target_chat_id
    twitter_user_id = context.user_data['twitter_user_id']
    rapidapi_key = RAPIDAPI_KEY  # from environment
    stop_event = threading.Event()
    monitor_flags[(target_chat_id, twitter_user_id)] = stop_event
    await update.message.reply_text("Configuration received. Starting tweet monitor. You will receive notifications here.")
    threading.Thread(target=monitor_tweets, args=(twitter_user_id, rapidapi_key, target_chat_id, stop_event, keywords), daemon=True).start()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Enter the Twitter user ID to stop monitoring, or type 'all' to stop all monitors for this chat.")
    return STOP_STATE

async def receive_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.message.chat.id
    input_text = update.message.text.strip()
    if input_text.lower() == "all":
        count = 0
        for key in list(monitor_flags.keys()):
            if key[0] == chat_id:
                monitor_flags[key].set()
                del monitor_flags[key]
                count += 1
        await update.message.reply_text(f"Stopped {count} monitors in this chat.")
    else:
        key = (chat_id, input_text)
        if key in monitor_flags:
            monitor_flags[key].set()
            del monitor_flags[key]
            await update.message.reply_text(f"Stopped monitoring Twitter user {input_text}.")
        else:
            await update.message.reply_text(f"No active monitor found for Twitter user {input_text}.")
    return ConversationHandler.END

# Create the telegram application (used for webhook processing)
telegram_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
conv_handler = ConversationHandler(
    entry_points=[CommandHandler("start", start)],
    states={
        TWITTER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_twitter_id)],
        KEYWORDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_keywords)],
    },
    fallbacks=[CommandHandler("cancel", cancel)]
)
telegram_app.add_handler(conv_handler)
stop_conv_handler = ConversationHandler(
    entry_points=[CommandHandler("stop", stop_command)],
    states={
        STOP_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_stop)]
    },
    fallbacks=[CommandHandler("cancel", cancel)]
)
telegram_app.add_handler(stop_conv_handler)

# -------------------- Flask App for Webhook --------------------
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Telegram Bot is running", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    update_data = request.get_json(force=True)
    logging.info("Received update: " + str(update_data))
    update_obj = Update.de_json(update_data, telegram_app.bot)
    # Process the update asynchronously
    asyncio.run(telegram_app.process_update(update_obj))
    return Response("OK", status=200)

# Set webhook on the first request
@app.before_first_request
def set_webhook():
    full_webhook_url = f"{WEBHOOK_URL}/webhook"
    logging.info(f"Setting webhook to {full_webhook_url}")
    result = telegram_app.bot.set_webhook(url=full_webhook_url)
    logging.info(f"Webhook set: {result}")

# Vercel requires the WSGI app to be named "handler" or "app"
handler = app
