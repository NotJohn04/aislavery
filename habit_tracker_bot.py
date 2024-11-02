# habit_tracker_bot.py

import os
from dotenv import load_dotenv
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from datetime import datetime, timedelta
import pytz
import re
import calendar
from config import get_sheets_service, SCOPES

# Load environment variables from .env file
load_dotenv()

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO  # Change to DEBUG for more detailed logs
)
logger = logging.getLogger(__name__)

# Get the bot token and spreadsheet ID from the environment variables
TOKEN = os.getenv('HABIT_TRACKER_BOT_TOKEN')
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')

if not TOKEN:
    raise ValueError("No token provided. Set the HABIT_TRACKER_BOT_TOKEN environment variable.")

if not SPREADSHEET_ID:
    raise ValueError("No spreadsheet ID provided. Set the SPREADSHEET_ID environment variable.")

# Define your habits
HABITS = [
    {
        'description': 'Morning Routine: Wake up, brush teeth, wash face, get ready',
        'time': '07:00',
        'duration': 30,  # Duration in minutes
        'frequency': 'daily',
    },
    {
        'description': 'Evening Routine: Bath, meditate, devotion, reflection',
        'time': '19:00',
        'duration': 60,
        'frequency': 'daily',
    },
    {
        'description': 'Sleep',
        'time': '23:59',
        'duration': 60,
        'frequency': 'daily',
    },
    {
        'description': 'Gym Workout',
        'time': '20:00',
        'duration': 90,
        'frequency': 'monday,thursday,saturday',
    },
    {
        'description': 'Basketball Game',
        'time': '20:00',
        'duration': 90,
        'frequency': 'wednesday',
    },
]

# Global dictionary to store user chat IDs (supports multiple users)
USER_CHAT_IDS = {}

# Error handler
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log the error and send a message to the user."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    if update and hasattr(update, "effective_message") and update.effective_message:
        try:
            await update.effective_message.reply_text("An unexpected error occurred. Please try again later.")
        except Exception as e:
            logger.error(f"Failed to send error message to user: {e}")

# Command: /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store user's chat ID and send welcome message."""
    user_id = update.effective_chat.id
    USER_CHAT_IDS[user_id] = update.effective_chat.id
    logger.info(f"User {user_id} started the Habit Tracker Bot.")
    await update.message.reply_text(
        "👋 Hi! I'm your Habit Tracker Bot.\n"
        "I can help you track your daily habits and remind you to complete them.\n\n"
        "Use /help to see available commands."
    )

# Command: /help
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /help is issued."""
    await update.message.reply_text(
        "Here are the commands you can use:\n"
        "/sethabits - View current habits\n"
        "/habitcheck - Manually trigger habit checks\n"
    )

# Command: /sethabits
async def set_habits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inform the user that habits are already set up."""
    habits_info = "Habits are already set up in the system.\n\nCurrent habits:\n"
    for habit in HABITS:
        freq = habit['frequency']
        if freq == 'daily':
            freq_info = 'Daily'
        else:
            freq_info = ', '.join([day.capitalize() for day in freq.split(',')])
        habits_info += f"- {habit['description']} ({freq_info})\n"
    await update.message.reply_text(habits_info)

# Function to schedule habits using JobQueue
def schedule_habits(app: Application):
    """Schedule habits based on their frequency and time using JobQueue."""
    local_tz = pytz.timezone('Asia/Kuala_Lumpur')
    days_map = {
        'monday': 'mon',
        'tuesday': 'tue',
        'wednesday': 'wed',
        'thursday': 'thu',
        'friday': 'fri',
        'saturday': 'sat',
        'sunday': 'sun'
    }

    for habit in HABITS:
        frequencies = [freq.strip().lower() for freq in habit['frequency'].split(',')]
        for freq in frequencies:
            if freq == 'daily':
                days_of_week = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
            elif freq in days_map:
                days_of_week = [days_map[freq]]
            else:
                logger.warning(f"Unknown frequency: {freq}")
                continue

            habit_time = datetime.strptime(habit['time'], '%H:%M').time()

            for day in days_of_week:
                # Calculate next run time
                now = datetime.now(local_tz)
                current_weekday = now.weekday()  # Monday is 0
                target_weekday = list(calendar.day_abbr).index(day.capitalize()[:3])  # e.g., 'mon' -> 0
                days_ahead = (target_weekday - current_weekday) % 7
                if days_ahead == 0 and now.time() > habit_time:
                    # If the time has already passed today, schedule for next week
                    days_ahead = 7
                run_date = now + timedelta(days=days_ahead)
                run_date = run_date.replace(hour=habit_time.hour, minute=habit_time.minute, second=0, microsecond=0)

                # Schedule the habit event
                app.job_queue.run_repeating(
                    callback=create_habit_event,
                    interval=timedelta(weeks=1),
                    first=run_date,
                    data=habit['description'],  # Use 'data' instead of 'context'
                    name=f"habit_{habit['description']}_{day}"
                )
                logger.info(f"Scheduled habit '{habit['description']}' on {day.capitalize()} at {habit['time']}")

# Function to create habit event
async def create_habit_event(context: ContextTypes.DEFAULT_TYPE):
    """Create a habit event in Sheets and schedule a completion check."""
    habit_description = context.job.data  # Access using 'data'
    try:
        local_tz = pytz.timezone('Asia/Kuala_Lumpur')
        now = datetime.now(local_tz)

        # Log in Google Sheets
        sheets_service = get_sheets_service()
        values = [[
            habit_description,
            'Pending',
            now.strftime('%Y-%m-%d'),
            now.strftime('%H:%M'),
            ''  # Event ID is optional for habits
        ]]

        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range='Habits!A:E',
            valueInputOption='RAW',
            body={'values': values}
        ).execute()

        # Retrieve the duration of the habit
        habit = next((h for h in HABITS if h['description'] == habit_description), None)
        habit_duration = habit['duration'] if habit else 30  # Default to 30 minutes if not found

        # Schedule completion check 30 minutes after habit duration
        reminder_time = now + timedelta(minutes=habit_duration + 30)
        app = context.application  # Access the application instance

        app.job_queue.run_once(
            send_habit_check,
            when=reminder_time,
            data=habit_description,  # Use 'data' instead of 'context'
            name=f"habit_check_{habit_description}_{now.strftime('%Y%m%d%H%M')}"
        )

        logger.info(f"Logged habit: {habit_description} at {now}")
        logger.info(f"Scheduled habit check at {reminder_time}")

    except Exception as e:
        logger.error(f"Error logging habit event: {e}")

# Function to send habit check
async def send_habit_check(context: ContextTypes.DEFAULT_TYPE):
    """Send a message to check if a habit was completed."""
    habit_description = context.job.data  # Access using 'data'

    # Assuming single-user bot. For multi-user, iterate through USER_CHAT_IDS
    if not USER_CHAT_IDS:
        logger.error("No chat IDs available. Make sure users have sent /start first.")
        return

    for user_id in USER_CHAT_IDS.values():
        keyboard = [
            [
                InlineKeyboardButton("✅ Yes", callback_data=f"habit_done|{habit_description}"),
                InlineKeyboardButton("❌ No", callback_data=f"habit_missed|{habit_description}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"Did you complete the habit '{habit_description}' today?",
                reply_markup=reply_markup
            )
            logger.info(f"Sent habit check for: {habit_description} to user {user_id}")
        except Exception as e:
            logger.error(f"Error sending habit check to user {user_id}: {e}")

# Callback handler for habit response
async def handle_habit_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user's response to habit completion check."""
    query = update.callback_query
    await query.answer()

    try:
        # Parse callback data
        data = query.data.split('|')
        if len(data) != 2:
            await query.edit_message_text("❌ Invalid response.")
            return

        status, habit_description = data

        # Get Sheets service
        sheets_service = get_sheets_service()

        # Read current data
        try:
            result = sheets_service.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID,
                range='Habits!A:E'
            ).execute()
            values = result.get('values', [])
        except Exception as e:
            logger.error(f"Error fetching data from Google Sheets: {e}")
            await query.edit_message_text("❌ An error occurred while accessing the habit list.")
            return

        if not values or len(values) < 2:
            await query.edit_message_text("❌ No habits found in the sheet.")
            return

        # Process sheet data
        headers = values[0]
        habits = [dict(zip(headers, row)) for row in values[1:]]
        logger.debug(f"Habits fetched: {habits}")

        # Find the habit
        row_number = None
        today_str = datetime.now(pytz.timezone('Asia/Kuala_Lumpur')).strftime('%Y-%m-%d')

        for idx, record in enumerate(habits, start=2):
            if (record.get('Habit Description', '').lower() == habit_description.lower() and
                    record.get('Date', '') == today_str):
                row_number = idx
                break

        if not row_number:
            await query.edit_message_text("❌ Habit not found in today's records.")
            return

        # Update status
        new_status = 'Done' if status == "habit_done" else 'Missed'
        try:
            sheets_service.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f'Habits!B{row_number}',
                valueInputOption='RAW',
                body={'values': [[new_status]]}
            ).execute()

            # Send confirmation
            emoji = "✅" if new_status == "Done" else "❌"
            await query.edit_message_text(
                f"{emoji} Habit '{habit_description}' marked as {new_status}!"
            )
            logger.info(f"Updated habit status: {habit_description} -> {new_status}")
        except Exception as e:
            logger.error(f"Error updating habit status: {e}")
            await query.edit_message_text("❌ An error occurred while updating the habit status.")

    except Exception as e:
        logger.error(f"Error handling habit response: {e}")
        await query.edit_message_text("❌ An error occurred while processing your response.")

# Command: /habitcheck
async def habit_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger habit checks for today."""
    sheets_service = get_sheets_service()
    try:
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range='Habits!A:E'
        ).execute()
        values = result.get('values', [])

        if not values or len(values) < 2:
            await update.message.reply_text("No habits found to check.")
            return

        headers = values[0]
        habits = [dict(zip(headers, row)) for row in values[1:]]
        today_str = datetime.now().strftime('%Y-%m-%d')
        todays_habits = [habit for habit in habits if habit['Date'] == today_str and habit['Status'] == 'Pending']

        if not todays_habits:
            await update.message.reply_text("All habits for today have been checked!")
            return

        for habit in todays_habits:
            await send_habit_check_directly(habit['Habit Description'])

    except Exception as e:
        logger.error(f"Error in /habitcheck: {e}")
        await update.message.reply_text("An unexpected error occurred. Please try again later.")

# Helper function to send habit check directly
async def send_habit_check_directly(habit_description: str):
    """Send a habit check message directly."""
    if not USER_CHAT_IDS:
        logger.error("No chat IDs available. Make sure users have sent /start first.")
        return

    for user_id in USER_CHAT_IDS.values():
        keyboard = [
            [
                InlineKeyboardButton("✅ Yes", callback_data=f"habit_done|{habit_description}"),
                InlineKeyboardButton("❌ No", callback_data=f"habit_missed|{habit_description}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await application.bot.send_message(
                chat_id=user_id,
                text=f"Did you complete the habit '{habit_description}' today?",
                reply_markup=reply_markup
            )
            logger.info(f"Sent manual habit check for: {habit_description} to user {user_id}")
        except Exception as e:
            logger.error(f"Error sending manual habit check to user {user_id}: {e}")

# Register all handlers
def register_handlers(app: Application):
    # Command handlers
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('sethabits', set_habits))
    app.add_handler(CommandHandler('habitcheck', habit_check))

    # Callback query handlers
    app.add_handler(CallbackQueryHandler(handle_habit_response, pattern='^habit_(done|missed)'))

    # Error handler
    app.add_error_handler(error_handler)

if __name__ == '__main__':
    # Initialize the application
    application = Application.builder().token(TOKEN).build()

    # Register handlers
    register_handlers(application)

    # Schedule habits using JobQueue
    schedule_habits(application)
    logger.info("Habits scheduled successfully")

    logger.info("Starting the Habit Tracker Bot...")
    application.run_polling()
