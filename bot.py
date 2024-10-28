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

# Command: /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Hi! I am your Productivity Bot. How can I assist you today?')

# First, add the natural language parsing function
def extract_duration(text):
    # Define regex patterns for duration
    duration_patterns = [
        r'for (\d+(?:\.\d+)?)\s*(hours|hour|hrs|hr|minutes|minute|mins|min)',
        r'in (\d+(?:\.\d+)?)\s*(hours|hour|hrs|hr|minutes|minute|mins|min)',
        r'lasting (\d+(?:\.\d+)?)\s*(hours|hour|hrs|hr|minutes|minute|mins|min)',
        r'(\d+(?:\.\d+)?)\s*(hours|hour|hrs|hr|minutes|minute|mins|min)\s*(long|duration)?'
    ]
    
    for pattern in duration_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            num = float(match.group(1))
            unit = match.group(2).lower()
            if 'hour' in unit or 'hr' in unit:
                return int(num * 60)  # Convert hours to minutes
            elif 'minute' in unit or 'min' in unit:
                return int(num)
    return None  # Duration not found

async def error_handler(update, context):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    # Notify the user about the error
    await update.message.reply_text("An unexpected error occurred. Please try again later.")

def parse_natural_language(text):
    duration_minutes = 60  # Default duration
    ambiguous = False

    # Extract dates and times with settings to prefer future dates
    date_times = search_dates(text, languages=['en'], settings={'PREFER_DATES_FROM': 'future'})
    event_datetime = None
    if date_times:
        # Choose the first date/time that is in the future
        now = datetime.now()
        future_dates = [(dt_text, dt) for dt_text, dt in date_times if dt > now]
        if future_dates:
            dt_text, event_datetime = future_dates[0]
            if len(future_dates) > 1:
                ambiguous = True  # Multiple future date/times found
        else:
            dt_text, event_datetime = date_times[0]
            ambiguous = True  # All dates are in the past
    else:
        ambiguous = True  # No date/time found

    # Extract duration
    extracted_duration = extract_duration(text)
    if extracted_duration:
        duration_minutes = extracted_duration

    # Remove date/time and duration phrases from text
    text_cleaned = text
    if date_times:
        for dt_text, _ in date_times:
            text_cleaned = re.sub(re.escape(dt_text), '', text_cleaned, flags=re.IGNORECASE)

    duration_phrases = re.findall(r'(for|in|lasting)?\s*\d+(?:\.\d+)?\s*(hours?|hrs?|minutes?|mins?)', text_cleaned, re.IGNORECASE)
    for phrase in duration_phrases:
        phrase_text = ' '.join(phrase).strip()
        text_cleaned = re.sub(re.escape(phrase_text), '', text_cleaned, flags=re.IGNORECASE)

    # Clean up extra spaces and punctuation
    event_description = re.sub(r'\s+', ' ', text_cleaned).strip().strip('.,')

    if not event_description or len(event_description.split()) < 2:
        ambiguous = True  # Description is too short

    return event_description, event_datetime, duration_minutes, ambiguous

# Add the event creation function
async def create_event(update, context, event_description, event_datetime, duration_minutes):
    # Localize datetime
    local_tz = pytz.timezone('Asia/Kuala_Lumpur')  # Replace with your timezone
    event_datetime = local_tz.localize(event_datetime)

    # Create Google Calendar event
    try:
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

    except Exception as e:
        logger.error(f"Error creating calendar event: {e}")
        await update.message.reply_text("❌ An error occurred while creating the event.")
        return

    # Log the event in Google Sheets
    try:
        sheets_service = get_sheets_service()
        values = [[event_description, 'Pending', event_datetime.strftime('%Y-%m-%d'), event_datetime.strftime('%H:%M'), event_id]]
        body = {'values': values}
        result = sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range='Events!A:E',
            valueInputOption='RAW',
            body=body
        ).execute()

    except Exception as e:
        logger.error(f"Error writing to Google Sheets: {e}")
        await update.message.reply_text("❌ An error occurred while writing to Google Sheets.")
        return

    await update.message.reply_text(
        f"✅ Event '{event_description}' has been created in your Google Calendar.\n"
        f"📅 Date and Time: {event_datetime.strftime('%Y-%m-%d %H:%M')} - {(event_datetime + timedelta(minutes=duration_minutes)).strftime('%H:%M')}\n"
        f"⏰ Duration: {duration_minutes} minutes"
    )

    # Schedule a reminder to check if the event was completed
    reminder_time = event_datetime + timedelta(minutes=duration_minutes)
    scheduler.add_job(
        send_event_check,
        trigger='date',
        run_date=reminder_time,
        args=[update.effective_chat.id, event_description, event_id],
        id=event_id
    )

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
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes", callback_data=f"event_done|{event_id}"),
            InlineKeyboardButton("❌ No", callback_data=f"event_missed|{event_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await application.bot.send_message(
        chat_id=chat_id,
        text=f"Did you complete the event '{event_description}'?",
        reply_markup=reply_markup
    )

# Callback handler for event completion
async def handle_event_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('|')
    if len(data) != 2:
        await query.edit_message_text("❌ Invalid response.")
        return

    status, event_id = data

    try:
        # Get Sheets service
        sheets_service = get_sheets_service()

        # Read all data from Events sheet
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range='Events!A:E'  # Adjust range based on your sheet columns
        ).execute()
        values = result.get('values', [])

        # Validate sheet data
        if not values or len(values) < 2:  # Check for headers and at least one row
            await query.edit_message_text("❌ No events found in the sheet.")
            return

        # Convert sheet data to dictionary format
        headers = values[0]
        events = [dict(zip(headers, row)) for row in values[1:]]

        # Find matching event row
        row_number = None
        event_description = None
        for idx, record in enumerate(events, start=2):  # Start from 2 to account for header row
            if record.get('Event ID') == event_id:
                row_number = idx
                event_description = record.get('Event Description')
                break

        if not row_number:
            await query.edit_message_text("❌ Event not found in the sheet.")
            return

        # Update status based on callback
        new_status = 'Done' if status == "event_done" else 'Missed'
        
        # Update the cell using Sheets API
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f'Events!B{row_number}',  # Status column
            valueInputOption='RAW',
            body={'values': [[new_status]]}
        ).execute()

        # Send confirmation message
        emoji = "✅" if status == "event_done" else "❌"
        await query.edit_message_text(
            f"{emoji} Event '{event_description}' marked as {new_status}!"
        )

    except Exception as e:
        logger.error(f"Error handling event response: {e}")
        await query.edit_message_text("❌ An error occurred while updating the event status.")

# Command: /settask
async def set_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Please enter the task details in the following format:\n"
        "/settask [Task Description] | [Due Date YYYY-MM-DD HH:MM]"
        "\n\nExample: /settask Finish report | 2024-10-25 17:00"
    )

# Handler for /settask
async def handle_set_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    pattern = r'^/settask\s+(.+?)\s*\|\s*(.+?)\s*$'
    match = re.match(pattern, text, re.IGNORECASE)
    if not match:
        await update.message.reply_text(
            "❌ Invalid format. Please use:\n"
            "/settask [Task Description] | [Due Date YYYY-MM-DD HH:MM]"
            "\n\nExample: /settask Finish report | 2024-10-25 17:00"
        )
        return

    task_description = match.group(1)
    due_date_str = match.group(2)
    due_date = dateparser.parse(due_date_str)

    if not due_date:
        await update.message.reply_text("❌ Could not parse the due date and time. Please ensure it's in a recognizable format.")
        return

    # Localize datetime
    local_tz = pytz.timezone('Asia/Kuala_Lumpur')  # Replace with your timezone
    due_date = local_tz.localize(due_date)

    # Log the task in Google Sheets
    try:
        sheet = get_google_sheet('Tasks')  # Ensure you have a 'Tasks' sheet
        sheet.append_row([task_description, 'Pending', due_date.strftime('%Y-%m-%d %H:%M'), ''])

        await update.message.reply_text(
            f"✅ Task '{task_description}' has been added with a due date of {due_date.strftime('%Y-%m-%d %H:%M')}."
        )

        # Schedule a reminder before the due date
        reminder_time = due_date - timedelta(minutes=30)  # 30 minutes before
        scheduler.add_job(
            send_task_reminder,
            trigger='date',
            run_date=reminder_time,
            args=[update.effective_chat.id, task_description, due_date],
            id=f"task_reminder_{task_description}_{due_date.strftime('%Y%m%d%H%M')}"
        )

    except Exception as e:
        logger.error(f"Error logging task: {e}")
        await update.message.reply_text("❌ An error occurred while adding the task.")

# Function to send task reminder
async def send_task_reminder(chat_id, task_description, due_date):
    keyboard = [
        [
            InlineKeyboardButton("✅ Completed", callback_data=f"task_done|{task_description}|{due_date.strftime('%Y-%m-%d %H:%M')}"),
            InlineKeyboardButton("❌ Not Yet", callback_data=f"task_not_done|{task_description}|{due_date.strftime('%Y-%m-%d %H:%M')}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await application.bot.send_message(
        chat_id=chat_id,
        text=f"⏰ Reminder: Task '{task_description}' is due at {due_date.strftime('%H:%M')}. Have you completed it?",
        reply_markup=reply_markup
    )

# Callback handler for task completion
async def handle_task_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('|')
    if len(data) != 3:
        await query.edit_message_text("❌ Invalid response.")
        return

    status, task_description, due_date_str = data
    sheet = get_google_sheet('Tasks')

    # Find the task row
    tasks = sheet.get_all_records()
    row_number = None
    for idx, record in enumerate(tasks, start=2):
        if (record['Task Description'].lower() == task_description.lower() and
                record['Due Date'] == due_date_str):
            row_number = idx
            break

    if not row_number:
        await query.edit_message_text("❌ Task not found in the sheet.")
        return

    if status == "task_done":
        sheet.update_cell(row_number, 2, 'Done')
        await query.edit_message_text(f"✅ Task '{task_description}' marked as Done!")
    elif status == "task_not_done":
        sheet.update_cell(row_number, 2, 'Pending')
        await query.edit_message_text(f"⏳ Task '{task_description}' remains Pending.")
    else:
        await query.edit_message_text("❌ Unknown status.")

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
    sheet = get_google_sheet('Events')
    events = sheet.get_all_records()
    today_str = datetime.now().strftime('%Y-%m-%d')
    todays_events = [event for event in events if event['Event Date'] == today_str and event['Status'] != 'Done']

    if not todays_events:
        await update.message.reply_text("🎉 You have no events for today! Enjoy your day!")
        return

    # Sort events by time
    todays_events.sort(key=lambda x: x['Event Time'])

    message = "📅 **Today's Events:**\n"
    for idx, event in enumerate(todays_events, start=1):
        message += f"{idx}. {event['Event Description']} at {event['Event Time']}\n"

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
                args=[habit_description, habit_time, duration_minutes],
                id=f"habit_event_{habit_description}_{freq}"
            )

    except Exception as e:
        logger.error(f"Error setting habit: {e}")
        await update.message.reply_text("❌ An error occurred while setting the habit.")

# Function to create habit event in Google Calendar
async def create_habit_event(habit_description, habit_time, duration_minutes):
    try:
        service = get_calendar_service()
        local_tz = pytz.timezone('Asia/Kuala_Lumpur')  # Replace with your timezone

        now = datetime.now(local_tz)
        event_datetime = now.replace(hour=habit_time.hour, minute=habit_time.minute, second=0, microsecond=0)
        if event_datetime < now:
            event_datetime += timedelta(days=1)

        event = {
            'summary': habit_description,
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
        sheet = get_google_sheet('HabitsEvents')  # Ensure you have a 'HabitsEvents' sheet
        sheet.append_row([habit_description, 'Pending', event_datetime.strftime('%Y-%m-%d'), event_datetime.strftime('%H:%M'), event_id])

        # Schedule a reminder to check habit completion
        reminder_time = event_datetime + timedelta(minutes=duration_minutes)
        scheduler.add_job(
            send_habit_check,
            trigger='date',
            run_date=reminder_time,
            args=[event_id, habit_description],
            id=f"habit_reminder_{event_id}"
        )

    except Exception as e:
        logger.error(f"Error creating habit event: {e}")

# Function to send habit check
async def send_habit_check(event_id, habit_description):
    sheet = get_google_sheet('HabitsEvents')
    events = sheet.get_all_records()
    event = next((e for e in events if e['Event ID'] == event_id), None)

    if not event:
        logger.warning(f"Habit event with ID {event_id} not found.")
        return

    keyboard = [
        [
            InlineKeyboardButton("✅ Yes", callback_data=f"habit_done|{event_id}"),
            InlineKeyboardButton("❌ No", callback_data=f"habit_missed|{event_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await application.bot.send_message(
        chat_id=YOUR_TELEGRAM_CHAT_ID,  # Replace with your actual Telegram chat ID
        text=f"Did you complete the habit '{habit_description}'?",
        reply_markup=reply_markup
    )

# Callback handler for habit response
async def handle_habit_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('|')
    if len(data) != 2:
        await query.edit_message_text("❌ Invalid response.")
        return

    status, event_id = data
    sheet = get_google_sheet('HabitsEvents')

    # Find the event row
    events = sheet.get_all_records()
    row_number = None
    for idx, record in enumerate(events, start=2):
        if record['Event ID'] == event_id:
            row_number = idx
            break

    if not row_number:
        await query.edit_message_text("❌ Habit event not found in the sheet.")
        return

    if status == "habit_done":
        sheet.update_cell(row_number, 2, 'Done')
        await query.edit_message_text(f"✅ Habit '{events[row_number - 2]['Habit Description']}' marked as Done!")
    elif status == "habit_missed":
        sheet.update_cell(row_number, 2, 'Missed')
        await query.edit_message_text(f"❌ Habit '{events[row_number - 2]['Habit Description']}' marked as Missed!")
    else:
        await query.edit_message_text("❌ Unknown status.")

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
        await send_habit_check(habit['Event ID'], habit['Habit Description'])

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

# Main function to start the bot
if __name__ == '__main__':
    application = Application.builder().token(TOKEN).build()
    register_handlers(application)
    application.run_polling()

