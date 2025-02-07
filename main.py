import os
import sys
import requests
import time
from datetime import datetime
import pytz
import threading
import re
import logging

# Set up logging to output to stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout
)

# Import classes from python-telegram-bot v21+
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# -----------------------------------------------------------------------------
# Load configuration from environment variables.
# Ensure these are set in your deployment environment (e.g. Vercel).
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY")

if not TELEGRAM_BOT_TOKEN:
    logging.error("TELEGRAM_BOT_TOKEN is not set in the environment variables.")
if not RAPIDAPI_KEY:
    logging.error("RAPIDAPI_KEY is not set in the environment variables.")

# Global dictionary to track monitoring stop events.
# Key: (chat_id, twitter_user_id), Value: threading.Event instance.
monitor_flags = {}

# --------------------- Tweet Scraper Code ---------------------

def get_tweet_text(tweet_data):
    """Extract full_text from the complex tweet JSON structure."""
    try:
        return tweet_data['result']['timeline']['instructions'][1]['entries'][0] \
            ['content']['itemContent']['tweet_results']['result']['legacy']['full_text']
    except (KeyError, IndexError):
        return None

def get_tweet_id(tweet_data):
    """Extract tweet ID from the complex tweet JSON structure."""
    try:
        return tweet_data['result']['timeline']['instructions'][1]['entries'][0] \
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

def send_telegram_message(bot_token, chat_id, message):
    """Send a message to a Telegram chat using the Bot API."""
    telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': message
    }
    try:
        response = requests.post(telegram_url, data=payload)
        if response.status_code != 200:
            logging.error(f"Failed to send Telegram message: {response.text}")
    except Exception as e:
        logging.error(f"Error sending Telegram message: {e}")

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
                    # Filter by keywords if provided
                    if keywords:
                        if not any(keyword.lower() in tweet_text.lower() for keyword in keywords):
                            logging.info(f"Tweet {tweet_id} does not contain any filter keywords. Skipping.")
                            time.sleep(5)
                            continue
                    latest_tweet_id = tweet_id
                    timestamp = get_ist_time()
                    # Retweet detection
                    retweet_flag = tweet_text.startswith("RT ")
                    retweet_text = "Retweet" if retweet_flag else "Original Tweet"
                    # Hashtag detection
                    hashtags = re.findall(r"#\w+", tweet_text)
                    hashtags_text = ", ".join(hashtags) if hashtags else "None"
                    # Construct tweet link (using Twitter's generic link format)
                    tweet_link = f"https://twitter.com/i/web/status/{tweet_id}"
                    message = (
                        f"[{timestamp}] New tweet detected!\n"
                        f"Twitter User: {twitter_user_id}\n"
                        f"Tweet ID: {tweet_id}\n"
                        f"Type: {retweet_text}\n"
                        f"Hashtags: {hashtags_text}\n"
                        f"Content: {tweet_text}\n"
                        f"Link: {tweet_link}\n"
                        + "-" * 50
                    )
                    send_telegram_message(TELEGRAM_BOT_TOKEN, target_chat_id, message)
            time.sleep(5)
        except Exception as e:
            error_message = f"An error occurred in tweet monitor for {twitter_user_id}: {e}"
            logging.error(error_message)
            send_telegram_message(TELEGRAM_BOT_TOKEN, target_chat_id, error_message)
            time.sleep(5)
    logging.info(f"Stopped monitoring tweets for {twitter_user_id} in chat {target_chat_id}.")

# ------------------ End of Tweet Scraper Code ----------------------

# -----------------------------------------------------------------------------
# Telegram Bot Conversation Handler States for /start
# States: TWITTER_ID, KEYWORDS
TWITTER_ID, KEYWORDS = range(2)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the /start command and begin the configuration conversation."""
    await update.message.reply_text("Welcome! Please send me the Twitter user ID you want to monitor.")
    return TWITTER_ID

async def receive_twitter_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the Twitter user ID from the user."""
    twitter_user_id = update.message.text.strip()
    context.user_data['twitter_user_id'] = twitter_user_id
    await update.message.reply_text("Optional: Enter filter keywords separated by commas, or type 'none' to monitor all tweets.")
    return KEYWORDS

async def receive_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the filter keywords and start the tweet monitor."""
    text = update.message.text.strip()
    if text.lower() == 'none':
        keywords = []
    else:
        keywords = [word.strip() for word in text.split(",") if word.strip()]
    context.user_data['keywords'] = keywords

    # Use the Telegram chat ID as the target for notifications.
    target_chat_id = update.message.chat.id
    context.user_data['target_chat_id'] = target_chat_id

    twitter_user_id = context.user_data['twitter_user_id']
    # Use the RapidAPI key from the environment variable.
    rapidapi_key = RAPIDAPI_KEY

    # Create a stop event for this monitoring thread.
    stop_event = threading.Event()
    # Store the stop event in the global dictionary for later stopping.
    monitor_flags[(target_chat_id, twitter_user_id)] = stop_event

    await update.message.reply_text("Configuration received. Starting tweet monitor. You will receive notifications here.")
    # Start tweet monitoring in a separate thread so that the Telegram bot stays responsive.
    threading.Thread(
        target=monitor_tweets,
        args=(twitter_user_id, rapidapi_key, target_chat_id, stop_event, keywords),
        daemon=True
    ).start()

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the configuration conversation."""
    await update.message.reply_text("Configuration canceled.")
    return ConversationHandler.END

# -----------------------------------------------------------------------------
# Telegram Bot Conversation Handler States for /stop
# We use a simple conversation to stop monitoring.
STOP = 0

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the /stop command by asking which Twitter user to stop monitoring."""
    await update.message.reply_text("Please enter the Twitter user ID to stop monitoring, or type 'all' to stop all monitors for this chat.")
    return STOP

async def receive_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process the /stop command input and stop the corresponding monitors."""
    chat_id = update.message.chat.id
    input_text = update.message.text.strip()
    if input_text.lower() == "all":
        count = 0
        for key in list(monitor_flags.keys()):
            if key[0] == chat_id:
                monitor_flags[key].set()  # Signal the thread to stop.
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

def main():
    """Set up the Telegram bot and start polling for commands."""
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Conversation handler for starting monitoring (/start)
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            TWITTER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_twitter_id)],
            KEYWORDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_keywords)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(conv_handler)

    # Conversation handler for stopping monitoring (/stop)
    stop_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("stop", stop_command)],
        states={
            STOP: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_stop)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(stop_conv_handler)

    logging.info("Bot started and polling for commands...")
    # Start the bot (this call blocks until you stop the bot)
    application.run_polling()

if __name__ == '__main__':
    main()
