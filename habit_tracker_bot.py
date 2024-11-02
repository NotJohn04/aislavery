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
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from typing import Optional

# Load environment variables from .env file
load_dotenv()

# Enable logging (Set to DEBUG for detailed logs)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO  # Change to DEBUG for more detailed logs
)
logger = logging.getLogger(__name__)

# Get the bot token and spreadsheet ID from the environment variables
TOKEN = os.getenv('HABIT_TRACKER_BOT_TOKEN')  # Ensure this is set correctly
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')  # Ensure this is set correctly

if not TOKEN:
    raise ValueError("No token provided. Set the TELEGRAM_BOT_TOKEN environment variable.")

if not SPREADSHEET_ID:
    raise ValueError("No spreadsheet ID provided. Set the SPREADSHEET_ID environment variable.")

# Define the scopes for Google APIs
SCOPES = [
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/spreadsheets'
]

# Initialize APScheduler
scheduler = AsyncIOScheduler(timezone='Asia/Kuala_Lumpur')  # Replace with your timezone
scheduler.start()

# Initialize the application globally
application = None

def get_credentials():
    """Get and refresh Google OAuth2 credentials."""
    try:
        creds = None
        if os.path.exists('token.json'):
            try:
                creds = Credentials.from_authorized_user_file('token.json', SCOPES)
                logger.info("Loaded existing credentials from token.json")
            except Exception as e:
                logger.error(f"Error loading token.json: {e}")
                os.remove('token.json')
                logger.info("Removed invalid token.json")

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("Refreshing expired credentials")
                creds.refresh(Request())
            else:
                if not os.path.exists('credentials.json'):
                    raise FileNotFoundError("credentials.json not found")

                logger.info("Initiating new OAuth flow")
                flow = InstalledAppFlow.from_client_secrets_file(
                    'credentials.json',
                    SCOPES
                )
                creds = flow.run_local_server(
                    port=0,
                    access_type='offline',
                    prompt='consent'
                )

            logger.info("Saving new credentials to token.json")
            with open('token.json', 'w') as token:
                token.write(creds.to_json())

        return creds

    except Exception as e:
        logger.error(f"Error in get_credentials: {e}")
        raise

def get_sheets_service():
    """Initialize Google Sheets API service."""
    creds = get_credentials()
    service = build('sheets', 'v4', credentials=creds)
    return service

def get_calendar_service():
    """Initialize Google Calendar API service."""
    creds = get_credentials()
    service = build('calendar', 'v3', credentials=creds)
    return service

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
        'frequency': 'tuesday,thursday,saturday',
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

# Define states for ConversationHandler
CONFIRMATION = 1

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

# Helper function to find the row number of a habit in Google Sheets
def get_habit_row(sheets_service, habit_description, date_str):
    """Find the row number of a habit in Google Sheets based on description and date."""
    try:
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range='Habits!A:E'
        ).execute()
        values = result.get('values', [])

        if not values or len(values) < 2:
            return None

        headers = values[0]
        for idx, row in enumerate(values[1:], start=2):
            row_dict = dict(zip(headers, row))
            if (row_dict.get('Habit Description', '').lower() == habit_description.lower() and
                    row_dict.get('Date', '') == date_str):
                return idx

        return None
    except Exception as e:
        logger.error(f"Error in get_habit_row: {e}")
        return None

# Function to create habit event
async def create_habit_event(habit_description: str, duration: int):
    """Create a habit event in Google Calendar and log it in Google Sheets."""
    logger.info(f"Executing create_habit_event for '{habit_description}' with duration {duration} minutes.")
    try:
        service = get_calendar_service()
        sheets_service = get_sheets_service()
        local_tz = pytz.timezone('Asia/Kuala_Lumpur')
        now = datetime.now(local_tz)

        # Create Google Calendar event
        event = {
            'summary': habit_description,
            'start': {
                'dateTime': now.isoformat(),
                'timeZone': str(local_tz),
            },
            'end': {
                'dateTime': (now + timedelta(minutes=duration)).isoformat(),
                'timeZone': str(local_tz),
            },
        }

        created_event = service.events().insert(calendarId='primary', body=event).execute()
        event_id = created_event.get('id')
        logger.info(f"Created Google Calendar event '{habit_description}' with ID {event_id}.")

        # Log the habit in Google Sheets
        values = [[
            habit_description,
            'Pending',
            now.strftime('%Y-%m-%d'),
            now.strftime('%H:%M'),
            event_id
        ]]

        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range='Habits!A:E',
            valueInputOption='RAW',
            body={'values': values}
        ).execute()
        logger.info(f"Logged habit '{habit_description}' in Google Sheets.")

        # Schedule a reminder to check habit completion
        reminder_time = now + timedelta(minutes=duration + 30)
        reminder_job_id = f"habit_check_{event_id}"

        scheduler.add_job(
            send_habit_check,
            trigger=DateTrigger(run_date=reminder_time),
            args=[habit_description, event_id],
            id=reminder_job_id,
            coalesce=True,  # Prevent overlapping jobs
            misfire_grace_time=300  # 5 minutes grace period
        )
        logger.info(f"Scheduled habit check for '{habit_description}' at {reminder_time}.")

    except Exception as e:
        logger.error(f"Error in create_habit_event: {e}")

# Function to send habit check
async def send_habit_check(habit_description: str, event_id: str):
    """Send a message to check if a habit was completed."""
    global application
    if not USER_CHAT_IDS:
        logger.error("No chat IDs available. Make sure users have sent /start first.")
        return

    for user_id in USER_CHAT_IDS.values():
        keyboard = [
            [
                InlineKeyboardButton("✅ Yes", callback_data=f"habit_done|{event_id}"),
                InlineKeyboardButton("❌ No", callback_data=f"habit_missed|{event_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await application.bot.send_message(
                chat_id=user_id,
                text=f"Did you complete the habit '{habit_description}' today?",
                reply_markup=reply_markup
            )
            logger.info(f"Sent habit check for: {habit_description} to user {user_id}")
        except Exception as e:
            logger.error(f"Error sending habit check to user {user_id}: {e}")

# Function to schedule habits two days in advance using APScheduler
def schedule_habits_two_days_ahead(app: Application):
    """Schedule habits two days in advance using APScheduler."""
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

    sheets_service = get_sheets_service()

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

            # Schedule for the next two days
            for day_offset in range(1, 3):
                run_date = datetime.now(local_tz) + timedelta(days=day_offset)
                target_weekday = run_date.weekday()  # Monday is 0
                day_abbr = calendar.day_abbr[target_weekday].lower()

                if day_abbr not in days_of_week:
                    continue  # Skip if the day is not in the frequency

                today_str = run_date.strftime('%Y-%m-%d')

                # Check if the habit is already scheduled for this date
                try:
                    result = sheets_service.spreadsheets().values().get(
                        spreadsheetId=SPREADSHEET_ID,
                        range='Habits!A:E'
                    ).execute()
                    values = result.get('values', [])
                    headers = values[0] if values else []
                    habits_logged = [dict(zip(headers, row)) for row in values[1:]]
                    already_scheduled = any(
                        habit_logged.get('Habit Description', '').lower() == habit['description'].lower() and
                        habit_logged.get('Date', '') == today_str
                        for habit_logged in habits_logged
                    )
                except Exception as e:
                    logger.error(f"Error fetching data from Google Sheets: {e}")
                    continue  # Skip scheduling if there's an error

                if already_scheduled:
                    logger.info(f"Habit '{habit['description']}' already scheduled for {today_str}. Skipping.")
                    continue  # Skip if already scheduled

                # Schedule the habit event
                run_datetime = run_date.replace(hour=habit_time.hour, minute=habit_time.minute, second=0, microsecond=0)
                job_id = f"habit_{habit['description']}_{today_str}"

                # Schedule the job only if it hasn't been scheduled yet
                if not scheduler.get_job(job_id):
                    scheduler.add_job(
                        create_habit_event,
                        trigger=DateTrigger(run_date=run_datetime),
                        args=[habit['description'], habit['duration']],
                        id=job_id,
                        coalesce=True,  # Prevent overlapping jobs
                        misfire_grace_time=300  # 5 minutes grace period
                    )
                    logger.info(f"Scheduled habit '{habit['description']}' for {today_str} at {habit['time']}")

    # Schedule a test habit 5 minutes from now
    test_run_datetime = datetime.now(local_tz) + timedelta(minutes=5)
    scheduler.add_job(
        create_habit_event,
        trigger=DateTrigger(run_date=test_run_datetime),
        args=["Test Habit", 10],  # Description and duration
        id="habit_Test_Habit_test_date",
        coalesce=True,
        misfire_grace_time=300
    )
    logger.info(f"Scheduled test habit 'Test Habit' for {test_run_datetime.strftime('%Y-%m-%d %H:%M:%S')}")

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

        status, event_id = data

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
        habit_description = None
        date_str = None
        for record in habits:
            if record.get('Event ID') == event_id:
                habit_description = record.get('Habit Description')
                date_str = record.get('Date')
                row_number = get_habit_row(sheets_service, habit_description, date_str)
                break

        if not row_number:
            await query.edit_message_text("❌ Habit not found in the sheet.")
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

            # Cancel the reminder job if it exists
            reminder_job_id = f"habit_check_{event_id}"
            if scheduler.get_job(reminder_job_id):
                scheduler.remove_job(reminder_job_id)
                logger.info(f"Removed reminder job {reminder_job_id}")

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
            await send_habit_check_directly(habit['Habit Description'], habit['Event ID'])

    except Exception as e:
        logger.error(f"Error in /habitcheck: {e}")
        await update.message.reply_text("An unexpected error occurred. Please try again later.")

# Helper function to send habit check directly
async def send_habit_check_directly(habit_description: str, event_id: str):
    """Send a habit check message directly."""
    if not USER_CHAT_IDS:
        logger.error("No chat IDs available. Make sure users have sent /start first.")
        return

    for user_id in USER_CHAT_IDS.values():
        keyboard = [
            [
                InlineKeyboardButton("✅ Yes", callback_data=f"habit_done|{event_id}"),
                InlineKeyboardButton("❌ No", callback_data=f"habit_missed|{event_id}")
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

    # Schedule habits two days ahead using APScheduler
    schedule_habits_two_days_ahead(application)
    logger.info("Habits scheduled successfully")

    logger.info("Starting the Habit Tracker Bot...")
    application.run_polling()
