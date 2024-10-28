import os
from dotenv import load_dotenv
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)
import dateparser
from datetime import datetime, timedelta
import re
import spacy
import pytz
import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
# import gspread
# from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from dateparser.search import search_dates

from typing import Optional

# Load environment variables from .env file
load_dotenv()

# Enable logging (Set to DEBUG for detailed logs)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO  # Change to DEBUG for more detailed logs
)
logger = logging.getLogger(__name__)

# Get the bot token from the environment variable
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')


if not TOKEN:
    raise ValueError("No token provided. Set the TELEGRAM_BOT_TOKEN environment variable.")

# Define the scope for Google Calendar
SCOPES_CALENDAR = ['https://www.googleapis.com/auth/calendar.events']

# Define the scope for Google Sheets
SCOPES_SHEETS = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive"
]
SCOPES = [
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/spreadsheets'
]


# Initialize spaCy's English model
nlp = spacy.load("en_core_web_sm")

# Initialize APScheduler
scheduler = AsyncIOScheduler(timezone='Asia/Kuala_Lumpur')  # Replace with your timezone
scheduler.start()

def get_credentials():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
        creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return creds

# # Initialize Google Sheets client
# def get_google_sheet(sheet_name):
#     creds = get_credentials()
#     client = gspread.authorize(creds)
#     sheet = client.open('ProductivityData').worksheet(sheet_name)  # Use specific sheet by name
#     return sheet_


def get_sheets_service():
    creds = get_credentials()
    service = build('sheets', 'v4', credentials=creds)
    return service



# Initialize Google Calendar service with OAuth 2.0
def get_calendar_service():
    creds = get_credentials()
    service = build('calendar', 'v3', credentials=creds)
    return service

# Helper function to get the application instance
def get_application():
    return application

# Global variables
USER_CHAT_ID: Optional[int] = None
application = None

# Command: /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store user's chat ID and send welcome message."""
    global USER_CHAT_ID
    USER_CHAT_ID = update.effective_chat.id
    logger.info(f"Chat ID set to: {USER_CHAT_ID}")
    await update.message.reply_text(
        "👋 Hi! I'm your Productivity Bot.\n"
        "I'll help you track your habits and tasks!\n\n"
        "Use /help to see available commands."
    )

# First, add the natural language parsing function
def extract_duration(text):
    """Extract duration in minutes from text."""
    duration_patterns = [
        r'for (\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)',
        r'(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\s*(long|duration)?',
        r'lasting (\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)',
    ]
    
    for pattern in duration_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            num = float(match.group(1))
            unit = match.group(2).lower()
            
            # Convert to minutes
            if unit.startswith(('hour', 'hr', 'h')):
                return int(num * 60)
            elif unit.startswith(('min', 'm')):
                return int(num)
                
    return 60  # Default duration in minutes

async def error_handler(update, context):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    # Notify the user about the error
    await update.message.reply_text("An unexpected error occurred. Please try again later.")

def parse_natural_language(text):
    """
    Parse natural language input for event details.
    Returns: (event_description, event_datetime, duration_minutes, ambiguous)
    """
    duration_minutes = 60  # Default duration
    ambiguous = False
    now = datetime.now()

    # First extract duration to prevent interference with date parsing
    extracted_duration = extract_duration(text)
    if extracted_duration:
        duration_minutes = extracted_duration

    # Remove duration phrases from text before date parsing
    duration_patterns = [
        r'for \d+(?:\.\d+)?\s*(hours?|hrs?|minutes?|mins?)',
        r'in \d+(?:\.\d+)?\s*(hours?|hrs?|minutes?|mins?)',
        r'lasting \d+(?:\.\d+)?\s*(hours?|hrs?|minutes?|mins?)',
        r'\d+(?:\.\d+)?\s*(hours?|hrs?|minutes?|mins?)\s*(long|duration)?'
    ]
    text_cleaned = text
    for pattern in duration_patterns:
        text_cleaned = re.sub(pattern, '', text_cleaned, flags=re.IGNORECASE)

    # Check for "now" explicitly
    now_patterns = [r'\bnow\b', r'\bright now\b', r'\bimmediately\b']
    is_now = any(re.search(pattern, text_cleaned, re.IGNORECASE) for pattern in now_patterns)
    
    if is_now:
        # Remove "now" related words from text
        for pattern in now_patterns:
            text_cleaned = re.sub(pattern, '', text_cleaned, flags=re.IGNORECASE)
        event_datetime = now
    else:
        # Extract dates and times for non-"now" cases
        date_times = search_dates(text_cleaned, languages=['en'], settings={
            'PREFER_DATES_FROM': 'future',
            'RELATIVE_BASE': now
        })
        
        if date_times:
            # Filter future dates
            future_dates = [(dt_text, dt) for dt_text, dt in date_times if dt > now]
            if future_dates:
                dt_text, event_datetime = future_dates[0]
                if len(future_dates) > 1:
                    ambiguous = True  # Multiple future dates found
            else:
                dt_text, event_datetime = date_times[0]
                ambiguous = True  # All dates are in the past
                
            # Remove the date/time text
            text_cleaned = re.sub(re.escape(dt_text), '', text_cleaned, flags=re.IGNORECASE)
        else:
            event_datetime = now  # Default to now if no date/time found
            ambiguous = True

    # Clean up the event description
    event_description = re.sub(r'\s+', ' ', text_cleaned).strip().strip('.,')
    
    # Additional cleanup for common artifacts
    event_description = re.sub(r'\b(set|schedule)\b', '', event_description, flags=re.IGNORECASE)
    event_description = re.sub(r'\s+', ' ', event_description).strip()

    # Mark as ambiguous if description is too short
    if not event_description or len(event_description.split()) < 2:
        ambiguous = True

    logger.debug(f"Parsed event: '{event_description}' at {event_datetime} for {duration_minutes} minutes (ambiguous: {ambiguous})")
    
    return event_description, event_datetime, duration_minutes, ambiguous

# Add the event creation function
async def create_event(update, context, event_description, event_datetime, duration_minutes):
    """Create an event and schedule its completion check."""
    # Localize datetime
    local_tz = pytz.timezone('Asia/Kuala_Lumpur')  # Replace with your timezone
    event_datetime = local_tz.localize(event_datetime)

    try:
        # Create Google Calendar event
        service = get_calendar_service()
        event = {
            'summary': event_description,
            'start': {
                'dateTime': event_datetime.isoformat(),
                'timeZone': str(local_tz),
            },
            'end': {
                'dateTime': (event_datetime + timedelta(minutes=duration_minutes)).isoformat(),
                'timeZone': str(local_tz),
            },
        }
        created_event = service.events().insert(calendarId='primary', body=event).execute()
        event_id = created_event.get('id')

        # Log the event in Google Sheets
        sheets_service = get_sheets_service()
        values = [[
            event_description, 
            'Pending', 
            event_datetime.strftime('%Y-%m-%d'), 
            event_datetime.strftime('%H:%M'),
            event_id
        ]]
        
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range='Events!A:E',
            valueInputOption='RAW',
            body={'values': values}
        ).execute()

        # Schedule the completion check
        reminder_time = event_datetime + timedelta(minutes=duration_minutes)
        scheduler.add_job(
            send_event_check,
            trigger='date',
            run_date=reminder_time,
            args=[update.effective_chat.id, event_description, event_id],
            id=event_id
        )

        # Send confirmation
        await update.message.reply_text(
            f"✅ Event '{event_description}' has been created.\n"
            f"📅 Date and Time: {event_datetime.strftime('%Y-%m-%d %H:%M')} - "
            f"{(event_datetime + timedelta(minutes=duration_minutes)).strftime('%H:%M')}\n"
            f" Duration: {duration_minutes} minutes"
        )

    except Exception as e:
        logger.error(f"Error creating event: {e}")
        await update.message.reply_text("❌ An error occurred while creating the event.")

# Add the natural language handler
async def handle_natural_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    event_description, event_datetime, duration_minutes, ambiguous = parse_natural_language(text)

    if not event_datetime or not event_description:
        await update.message.reply_text(
            "❓ I couldn't understand your request. Please provide an event description and a date/time."
        )
        return ConversationHandler.END

    if ambiguous:
        # Ask for confirmation
        await update.message.reply_text(
            f"Please confirm the event details:\n"
            f"Description: {event_description}\n"
            f"Date and Time: {event_datetime.strftime('%Y-%m-%d %H:%M')}\n"
            f"Duration: {duration_minutes} minutes\n"
            f"Reply with 'yes' to confirm or 'no' to cancel or modify."
        )
        # Store the event details
        context.user_data['pending_event'] = {
            'description': event_description,
            'datetime': event_datetime,
            'duration': duration_minutes
        }
        return CONFIRMATION
    else:
        # Proceed to create the event directly
        await create_event(update, context, event_description, event_datetime, duration_minutes)
        return ConversationHandler.END

async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_response = update.message.text.strip().lower()
        
        if user_response not in ['yes', 'no']:
            await update.message.reply_text("❓ Please reply with 'yes' to confirm or 'no' to cancel.")
            return CONFIRMATION

        if not context.user_data.get('pending_event'):
            await update.message.reply_text("⚠️ No pending event found. Please start over with /setevent.")
            return ConversationHandler.END

        if user_response == 'yes':
            pending_event = context.user_data['pending_event']
            await create_event(
                update,
                context,
                pending_event['description'],
                pending_event['datetime'],
                pending_event['duration']
            )
        else:
            await update.message.reply_text("🛑 Event creation cancelled.")

        # Clear the pending event
        context.user_data.pop('pending_event', None)
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error in handle_confirmation: {e}")
        await update.message.reply_text("❌ An error occurred while processing your response. Please try again with /setevent.")
        return ConversationHandler.END

# Update the set_event command handler
async def set_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    command_removed = text.partition(' ')[2]  # Gets the text after the command

    if not command_removed:
        await update.message.reply_text(
            "Please enter the event details in either format:\n"
            "1. Natural language: /setevent Dinner with family tomorrow at 7pm\n"
            "2. Structured format: /setevent [Event Description] | [YYYY-MM-DD HH:MM] | [Duration in minutes]\n"
            "\nExample: /setevent Dinner with family | 2024-10-25 19:30 | 60"
        )
        return ConversationHandler.END

    # First try structured format
    pattern = r'^(.+?)\s*\|\s*(.+?)\s*\|\s*(\d+)\s*$'
    match = re.match(pattern, command_removed, re.IGNORECASE)

    try:
        if match:
            # Process structured format
            event_description = match.group(1).strip()
            event_datetime_str = match.group(2).strip()
            duration_minutes = int(match.group(3))
            event_datetime = dateparser.parse(event_datetime_str)

            if not event_datetime:
                await update.message.reply_text("❌ Could not parse the date and time. Please ensure it's in a recognizable format.")
                return ConversationHandler.END

            # Store event details and proceed to confirmation
            context.user_data['pending_event'] = {
                'description': event_description,
                'datetime': event_datetime,
                'duration': duration_minutes
            }
        else:
            # Try natural language parsing
            event_description, event_datetime, duration_minutes, ambiguous = parse_natural_language(command_removed)

            if not event_datetime or not event_description:
                await update.message.reply_text(
                    "❓ I couldn't understand your request. Please provide an event description and a date/time."
                )
                return ConversationHandler.END

            # Store event details
            context.user_data['pending_event'] = {
                'description': event_description,
                'datetime': event_datetime,
                'duration': duration_minutes
            }

        # Always ask for confirmation
        await update.message.reply_text(
            f"Please confirm the event details:\n"
            f"Description: {context.user_data['pending_event']['description']}\n"
            f"Date and Time: {context.user_data['pending_event']['datetime'].strftime('%Y-%m-%d %H:%M')}\n"
            f"Duration: {context.user_data['pending_event']['duration']} minutes\n"
            f"Reply with 'yes' to confirm or 'no' to cancel."
        )
        return CONFIRMATION

    except Exception as e:
        logger.error(f"Error in set_event: {e}")
        await update.message.reply_text("❌ An error occurred while processing your request. Please try again.")
        return ConversationHandler.END

# Function to send event completion check
async def send_event_check(chat_id, event_description, event_id):
    """Send a message to check if an event was completed."""
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes", callback_data=f"event_done|{event_id}"),
            InlineKeyboardButton("❌ No", callback_data=f"event_missed|{event_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        await application.bot.send_message(
            chat_id=chat_id,
            text=f"⏰ Your event '{event_description}' has ended.\nDid you complete it?",
            reply_markup=reply_markup
        )
        
        # Schedule auto-update after 1 hour if no response
        auto_update_time = datetime.now() + timedelta(hours=1)
        scheduler.add_job(
            auto_update_event_status,
            trigger='date',
            run_date=auto_update_time,
            args=[event_id],
            id=f"auto_update_{event_id}"
        )
    except Exception as e:
        logger.error(f"Error sending event check: {e}")

async def auto_update_event_status(event_id):
    """Automatically update event status to 'Missed' if no response after timeout."""
    try:
        sheets_service = get_sheets_service()

        # Read current data
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range='Events!A:E'
        ).execute()
        values = result.get('values', [])

        if not values or len(values) < 2:
            logger.warning("No events found in sheet for auto-update")
            return

        # Find the event
        headers = values[0]
        events = [dict(zip(headers, row)) for row in values[1:]]
        
        row_number = None
        for idx, record in enumerate(events, start=2):
            if record.get('Event ID') == event_id and record.get('Status') == 'Pending':
                row_number = idx
                break

        if row_number:
            # Update to 'Missed' if still pending
            sheets_service.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f'Events!B{row_number}',
                valueInputOption='RAW',
                body={'values': [['Missed']]}
            ).execute()
            logger.info(f"Event {event_id} auto-updated to 'Missed' due to no response")

    except Exception as e:
        logger.error(f"Error in auto_update_event_status: {e}")

async def handle_event_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user's response to event completion check."""
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
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range='Events!A:E'
        ).execute()
        values = result.get('values', [])

        if not values or len(values) < 2:
            await query.edit_message_text("❌ No events found in the sheet.")
            return

        # Process sheet data
        headers = values[0]
        events = [dict(zip(headers, row)) for row in values[1:]]

        # Find the event
        row_number = None
        event_description = None
        for idx, record in enumerate(events, start=2):
            if record.get('Event ID') == event_id:
                row_number = idx
                event_description = record.get('Event Description')
                break

        if not row_number:
            await query.edit_message_text("❌ Event not found in the sheet.")
            return

        # Update status
        new_status = 'Done' if status == "event_done" else 'Missed'
        
        # Update the sheet
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f'Events!B{row_number}',  # Status column
            valueInputOption='RAW',
            body={'values': [[new_status]]}
        ).execute()

        # Cancel auto-update job if it exists
        try:
            scheduler.remove_job(f"auto_update_{event_id}")
        except Exception:
            pass  # Job might not exist, that's okay

        # Send confirmation
        emoji = "✅" if status == "event_done" else "❌"
        await query.edit_message_text(
            f"{emoji} Event '{event_description}' marked as {new_status}!"
        )

    except Exception as e:
        logger.error(f"Error handling event response: {e}")
        await query.edit_message_text("❌ An error occurred while updating the event status.")

# Update the set_task function and its handlers
async def set_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /settask command."""
    text = update.message.text
    pattern = r'^/settask\s+(.+?)\s*\|\s*(.+?)\s*$'
    match = re.match(pattern, text, re.IGNORECASE)
    
    if not match:
        await update.message.reply_text(
            "❌ Invalid format. Please use:\n"
            "/settask [Task Description] | [Due Date]\n\n"
            "Examples:\n"
            "• /settask Complete project report | tomorrow 5pm\n"
            "• /settask Buy groceries | 2024-03-15 14:00\n"
            "• /settask Call client | next monday 10am"
        )
        return

    task_description = match.group(1).strip()
    due_date_str = match.group(2).strip()
    
    # Parse the due date
    try:
        due_date = dateparser.parse(
            due_date_str,
            settings={
                'PREFER_DATES_FROM': 'future',
                'TIMEZONE': 'Asia/Kuala_Lumpur',
                'RETURN_AS_TIMEZONE_AWARE': True
            }
        )
        
        if not due_date:
            await update.message.reply_text(
                "❌ Could not understand the due date format.\n"
                "Please use a clear date and time format."
            )
            return
            
        # Get current time in local timezone
        local_tz = pytz.timezone('Asia/Kuala_Lumpur')
        now = datetime.now(local_tz)
        
        # Ensure due date is in the future
        if due_date <= now:
            await update.message.reply_text(
                "❌ Due date must be in the future."
            )
            return

        # Add to Google Sheets
        sheets_service = get_sheets_service()
        values = [[
            task_description,
            'Pending',
            due_date.strftime('%Y-%m-%d %H:%M'),
            now.strftime('%Y-%m-%d %H:%M')
        ]]

        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range='Tasks!A:D',
            valueInputOption='RAW',
            body={'values': values}
        ).execute()

        # Schedule a reminder (30 minutes before due date)
        reminder_time = due_date - timedelta(minutes=30)
        if reminder_time > now:
            scheduler.add_job(
                send_task_reminder,
                'date',
                run_date=reminder_time,
                args=[update.effective_chat.id, task_description, due_date],
                id=f"task_reminder_{task_description}_{due_date.strftime('%Y%m%d%H%M')}"
            )

        # Send confirmation
        await update.message.reply_text(
            f"✅ Task added successfully!\n\n"
            f"📝 Task: {task_description}\n"
            f"⏰ Due: {due_date.strftime('%Y-%m-%d %H:%M')}\n"
            f"🔔 Reminder set for: {reminder_time.strftime('%Y-%m-%d %H:%M')}"
        )

    except Exception as e:
        logger.error(f"Error in set_task: {e}")
        await update.message.reply_text(
            "❌ An error occurred while adding the task.\n"
            "Please try again or contact support if the issue persists."
        )

async def send_task_reminder(chat_id: int, task_description: str, due_date: datetime):
    """Send a reminder for a task that's due soon."""
    keyboard = [
        [
            InlineKeyboardButton("✅ Done", callback_data=f"task_done|{task_description}"),
            InlineKeyboardButton("⏰ Snooze", callback_data=f"task_snooze|{task_description}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        await application.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔔 Reminder: Task due in 30 minutes!\n\n"
                f"📝 Task: {task_description}\n"
                f"⏰ Due at: {due_date.strftime('%H:%M')}"
            ),
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Error sending task reminder: {e}")

async def handle_task_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user's response to task completion check."""
    query = update.callback_query
    await query.answer()
    
    try:
        action, task_description = query.data.split('|')
        
        if action == "task_done":
            # Update task status in sheets
            sheets_service = get_sheets_service()
            result = sheets_service.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID,
                range='Tasks!A:D'
            ).execute()
            
            values = result.get('values', [])
            row_number = None
            
            for idx, row in enumerate(values[1:], start=2):
                if row[0] == task_description and row[1] == 'Pending':
                    row_number = idx
                    break
            
            if row_number:
                sheets_service.spreadsheets().values().update(
                    spreadsheetId=SPREADSHEET_ID,
                    range=f'Tasks!B{row_number}',
                    valueInputOption='RAW',
                    body={'values': [['Done']]}
                ).execute()
                
                await query.edit_message_text(
                    f"✅ Task '{task_description}' marked as Done!"
                )
            else:
                await query.edit_message_text(
                    f"❌ Could not find pending task: {task_description}"
                )
                
        elif action == "task_snooze":
            # Snooze for 15 minutes
            snooze_time = datetime.now(pytz.timezone('Asia/Kuala_Lumpur')) + timedelta(minutes=15)
            scheduler.add_job(
                send_task_reminder,
                'date',
                run_date=snooze_time,
                args=[update.effective_chat.id, task_description, snooze_time + timedelta(minutes=30)],
                id=f"task_reminder_{task_description}_{snooze_time.strftime('%Y%m%d%H%M')}"
            )
            
            await query.edit_message_text(
                f"⏰ Reminder snoozed for 15 minutes"
            )
            
    except Exception as e:
        logger.error(f"Error handling task response: {e}")
        await query.edit_message_text("❌ An error occurred while processing your response.")

# Command: /tasktoday
async def task_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sheet = get_google_sheet('Tasks')
    tasks = sheet.get_all_records()
    today_str = datetime.now().strftime('%Y-%m-%d')
    todays_tasks = [task for task in tasks if task['Due Date'].startswith(today_str) and task['Status'] != 'Done']

    if not todays_tasks:
        await update.message.reply_text("🎉 You have no tasks for today! Great job!")
        return

    # Sort tasks by due time
    todays_tasks.sort(key=lambda x: x['Due Date'])

    message = "📝 **Today's Tasks:**\n"
    for idx, task in enumerate(todays_tasks, start=1):
        message += f"{idx}. {task['Task Description']} (Due: {task['Due Date']})\n"

    await update.message.reply_text(message)

# Command: /eventtoday
async def event_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service = get_calendar_service()

    # Define the time range for today in your local timezone
    local_tz = pytz.timezone('Asia/Kuala_Lumpur')  # Replace with your timezone if different
    now = datetime.now(local_tz)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)

    # Convert to RFC3339 timestamp format with timezone offset
    time_min = start_of_day.isoformat()
    time_max = end_of_day.isoformat()

    # Fetch events from Google Calendar within the specified time range
    events_result = service.events().list(
        calendarId='primary',
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy='startTime'
    ).execute()

    events = events_result.get('items', [])

    if not events:
        await update.message.reply_text("🎉 You have no events for today! Enjoy your day!")
        return

    message = "📅 **Today's Events:**\n"
    for idx, event in enumerate(events, start=1):
        # Get the event start time
        start = event['start'].get('dateTime', event['start'].get('date'))
        # Parse and format the event time
        event_time = dateparser.parse(start).astimezone(local_tz).strftime('%H:%M')
        # Get the event summary or title
        summary = event.get('summary', 'No Title')
        message += f"{idx}. {summary} at {event_time}\n"

    await update.message.reply_text(message)

# Command: /sethabits
async def set_habits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Please enter your habits in the following format:\n"
        "/sethabits [Habit Description] | [Frequency: daily, monday, tuesday, ...] | [Time HH:MM] | [Duration in minutes]"
        "\n\nExample: /sethabits Meditate | daily | 07:00 | 30"
    )

# Handler for /sethabits
async def handle_set_habits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    pattern = r'^/sethabits\s+(.+?)\s*\|\s*(.+?)\s*\|\s*(\d{1,2}:\d{2})\s*\|\s*(\d+)\s*$'
    match = re.match(pattern, text, re.IGNORECASE)
    if not match:
        await update.message.reply_text(
            "❌ Invalid format. Please use:\n"
            "/sethabits [Habit Description] | [Frequency: daily, monday, tuesday, ...] | [Time HH:MM] | [Duration in minutes]"
            "\n\nExample: /sethabits Meditate | daily | 07:00 | 30"
        )
        return

    habit_description = match.group(1)
    frequency = match.group(2).lower()
    time_str = match.group(3)
    duration_minutes = int(match.group(4))

    time_parsed = dateparser.parse(time_str)
    if not time_parsed:
        await update.message.reply_text("❌ Could not parse the time. Please ensure it's in HH:MM format.")
        return

    # Localize time
    local_tz = pytz.timezone('Asia/Kuala_Lumpur')  # Replace with your timezone
    habit_time = local_tz.localize(time_parsed.replace(year=datetime.now().year, month=datetime.now().month, day=datetime.now().day))

    # Log the habit in Google Sheets
    try:
        sheet = get_google_sheet('Habits')  # Ensure you have a 'Habits' sheet
        sheet.append_row([habit_description, frequency, habit_time.strftime('%H:%M'), duration_minutes])

        await update.message.reply_text(
            f"✅ Habit '{habit_description}' has been set for {frequency} at {habit_time.strftime('%H:%M')} for {duration_minutes} minutes."
        )

        # Schedule recurring Google Calendar events based on frequency
        frequencies = [freq.strip().lower() for freq in frequency.split(',')]

        days_map = {
            'monday': 'mon',
            'tuesday': 'tue',
            'wednesday': 'wed',
            'thursday': 'thu',
            'friday': 'fri',
            'saturday': 'sat',
            'sunday': 'sun'
        }

        for freq in frequencies:
            if freq == 'daily':
                day_of_week = 'mon,tue,wed,thu,fri,sat,sun'
            elif freq in days_map:
                day_of_week = days_map[freq]
            else:
                logger.warning(f"Unknown frequency: {freq}")
                continue

            trigger = CronTrigger(hour=habit_time.hour, minute=habit_time.minute, day_of_week=day_of_week)

            scheduler.add_job(
                create_habit_event,
                trigger=trigger,
                args=[habit_description, duration_minutes],
                id=f"habit_event_{habit_description}_{freq}"
            )

    except Exception as e:
        logger.error(f"Error setting habit: {e}")
        await update.message.reply_text("❌ An error occurred while setting the habit.")

# Function to create habit event in Google Calendar
async def create_habit_event(habit_description: str, duration: int):
    """Create a habit event in Calendar and Sheets."""
    try:
        service = get_calendar_service()
        local_tz = pytz.timezone('Asia/Kuala_Lumpur')
        now = datetime.now(local_tz)
        
        # Create calendar event
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

        # Log in Google Sheets
        sheets_service = get_sheets_service()
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

        # Schedule completion check
        reminder_time = now + timedelta(minutes=duration)
        scheduler.add_job(
            send_habit_check,
            trigger='date',
            run_date=reminder_time,
            args=[habit_description, event_id],
            id=f"habit_check_{event_id}"
        )

        logger.info(f"Created habit event: {habit_description}")

    except Exception as e:
        logger.error(f"Error creating habit event: {e}")

# Function to send habit check
async def send_habit_check(habit_description: str, event_id: str):
    """Send a message to check if a habit was completed."""
    global USER_CHAT_ID, application
    
    if not USER_CHAT_ID:
        logger.error("No chat ID available. Make sure to run /start first.")
        return

    keyboard = [
        [
            InlineKeyboardButton("✅ Yes", callback_data=f"habit_done|{event_id}"),
            InlineKeyboardButton("❌ No", callback_data=f"habit_missed|{event_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await application.bot.send_message(
            chat_id=USER_CHAT_ID,
            text=f"Did you complete the habit '{habit_description}'?",
            reply_markup=reply_markup
        )
        logger.info(f"Sent habit check for: {habit_description}")
    except Exception as e:
        logger.error(f"Error sending habit check: {e}")

# Callback handler for habit response
async def handle_habit_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user's response to habit completion check."""
    query = update.callback_query
    await query.answer()
    
    try:
        status, event_id = query.data.split('|')
    except ValueError:
        await query.edit_message_text("❌ Invalid response format.")
        return

    try:
        # Get sheet data
        sheets_service = get_sheets_service()
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range='Habits!A:E'
        ).execute()
        values = result.get('values', [])

        if not values:
            await query.edit_message_text("❌ No habits found in sheet.")
            return

        # Find the habit row
        headers = values[0]
        row_number = None
        habit_description = None

        for idx, row in enumerate(values[1:], start=2):
            row_dict = dict(zip(headers, row))
            if row_dict.get('Event ID') == event_id:
                row_number = idx
                habit_description = row_dict.get('Habit Description')
                break

        if not row_number:
            await query.edit_message_text("❌ Habit not found in records.")
            return

        # Update status
        new_status = 'Done' if status == "habit_done" else 'Missed'
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f'Habits!B{row_number}',
            valueInputOption='RAW',
            body={'values': [[new_status]]}
        ).execute()

        # Confirm update
        emoji = "✅" if new_status == "Done" else "❌"
        await query.edit_message_text(
            f"{emoji} Habit '{habit_description}' marked as {new_status}!"
        )
        logger.info(f"Updated habit status: {habit_description} -> {new_status}")

    except Exception as e:
        logger.error(f"Error handling habit response: {e}")
        await query.edit_message_text("❌ An error occurred while updating the habit.")

# Command: /habitcheck
async def habit_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Fetch today's habits
    sheet = get_google_sheet('HabitsEvents')
    events = sheet.get_all_records()
    today_str = datetime.now().strftime('%Y-%m-%d')
    todays_habits = [event for event in events if event['Event Date'] == today_str and event['Status'] == 'Pending']

    if not todays_habits:
        await update.message.reply_text("All habits for today have been checked!")
        return

    for habit in todays_habits:
        await send_habit_check(habit['Habit Description'], habit['Event ID'])

# Define states
CONFIRMATION = 1

# Register all handlers
def register_handlers(application):
    # Command handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('tasktoday', task_today))
    application.add_handler(CommandHandler('eventtoday', event_today))
    application.add_handler(CommandHandler('sethabits', set_habits))
    application.add_handler(CommandHandler('habitcheck', habit_check))

    # Conversation handler for set_event
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('setevent', set_event)],
        states={
            CONFIRMATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_confirmation)],
        },
        fallbacks=[CommandHandler('cancel', lambda u, c: ConversationHandler.END)],
        name="event_conversation",
        persistent=False
    )
    application.add_handler(conv_handler)

    # Callback query handlers
    application.add_handler(CallbackQueryHandler(handle_event_response, pattern='^event_'))
    application.add_handler(CallbackQueryHandler(handle_task_response, pattern='^task_'))
    application.add_handler(CallbackQueryHandler(handle_habit_response, pattern='^habit_'))

    # Error handler
    application.add_error_handler(error_handler)

# Define habits configuration
HABITS = [
    # Daily Morning Routine
    {
        'description': 'Morning Routine: Wake up, brush teeth, wash face, get ready',
        'time': '07:00',
        'duration': 30,  # Duration in minutes
        'frequency': 'daily',
    },
    # Daily Evening Routine
    {
        'description': 'Evening Routine: Bath, meditate, devotion, reflection',
        'time': '19:00',
        'duration': 60,
        'frequency': 'daily',
    },
    # Gym on Tuesday, Thursday, Saturday
    {
        'description': 'Gym Workout',
        'time': '20:00',
        'duration': 90,
        'frequency': 'tuesday,thursday,saturday',
    },
    # Basketball on Wednesday
    {
        'description': 'Basketball Game',
        'time': '20:00',
        'duration': 90,
        'frequency': 'wednesday',
    },
]

def schedule_habits():
    """Schedule all predefined habits."""
    local_tz = pytz.timezone('Asia/Kuala_Lumpur')
    days_map = {
        'monday': 'mon', 'tuesday': 'tue', 'wednesday': 'wed',
        'thursday': 'thu', 'friday': 'fri', 'saturday': 'sat',
        'sunday': 'sun'
    }

    for habit in HABITS:
        frequencies = [freq.strip().lower() for freq in habit['frequency'].split(',')]
        for freq in frequencies:
            if freq == 'daily':
                day_of_week = 'mon,tue,wed,thu,fri,sat,sun'
            elif freq in days_map:
                day_of_week = days_map[freq]
            else:
                logger.warning(f"Unknown frequency: {freq}")
                continue

            time_parsed = dateparser.parse(habit['time'])
            habit_time = time_parsed.time()
            
            # Create cron trigger
            trigger = CronTrigger(
                day_of_week=day_of_week,
                hour=habit_time.hour,
                minute=habit_time.minute,
                timezone=local_tz
            )

            # Schedule the habit with correct arguments
            scheduler.add_job(
                create_habit_event,
                trigger=trigger,
                args=[habit['description'], habit['duration']],  # Pass both required arguments
                id=f"habit_{habit['description']}_{freq}",
                replace_existing=True
            )
            logger.info(f"Scheduled habit: {habit['description']} for {freq} at {habit['time']}")

# Main function to start the bot
if __name__ == '__main__':
    application = Application.builder().token(TOKEN).build()
    register_handlers(application)
    schedule_habits()
    logger.info("Habits scheduled successfully")
    application.run_polling()
