# event_manager_bot.py

import os
from dotenv import load_dotenv
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
import dateparser
from dateparser.search import search_dates
from datetime import datetime, timedelta
import pytz
import re
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from config import get_sheets_service, get_calendar_service, SCOPES

# Load environment variables from .env file
load_dotenv()

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG
)
logger = logging.getLogger(__name__)

# Get the bot token and spreadsheet ID from the environment variables
TOKEN = os.getenv('EVENT_MANAGER_BOT_TOKEN')
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')

if not TOKEN:
    raise ValueError("No token provided. Set the EVENT_MANAGER_BOT_TOKEN environment variable.")

if not SPREADSHEET_ID:
    raise ValueError("No spreadsheet ID provided. Set the SPREADSHEET_ID environment variable.")

# Initialize APScheduler
scheduler = AsyncIOScheduler(timezone='Asia/Kuala_Lumpur')
scheduler.start()

# Define states for ConversationHandler
CONFIRMATION = 1

# Command: /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store user's chat ID and send welcome message."""
    user_id = update.effective_chat.id
    logger.info(f"User {user_id} started the Event Manager Bot.")
    await update.message.reply_text(
        "👋 Hi! I'm your Event Manager Bot.\n"
        "I can help you schedule events, add them to your Google Calendar, and track their completion.\n\n"
        "Use /help to see available commands."
    )

# Command: /help
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /help is issued."""
    await update.message.reply_text(
        "Here are the commands you can use:\n"
        "/setevent - Schedule a new event\n"
        "/eventtoday - View today's events\n"
    )

# Function to extract duration from text
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

# Error handler
async def error_handler(update, context):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    # Notify the user about the error
    if update and update.effective_message:
        await update.effective_message.reply_text("An unexpected error occurred. Please try again later.")

# Function to parse natural language input for event details
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
        logger.debug(f"Extracted duration: {duration_minutes} minutes")

    # Remove duration phrases from text before date parsing
    duration_patterns = [
        r'for \d+(?:\.\d+)?\s*(hours?|hrs?|minutes?|mins?)',
        r'in \d+(?:\.\d+)?\s*(hours?|hrs?|minutes?|mins?)',
        r'lasting \d+(?:\.\d+)?\s*(hours?|hrs?|minutes?|mins?)',
        r'\d+(?:\.\d+)?\s*(hours?|hrs?|minutes?|mins?|m)\s*(long|duration)?'
    ]
    text_cleaned = text
    for pattern in duration_patterns:
        text_cleaned = re.sub(pattern, '', text_cleaned, flags=re.IGNORECASE)
        logger.debug(f"Text after removing duration pattern '{pattern}': '{text_cleaned}'")

    # Check for "now" explicitly
    now_patterns = [r'\bnow\b', r'\bright now\b', r'\bimmediately\b']
    is_now = any(re.search(pattern, text_cleaned, re.IGNORECASE) for pattern in now_patterns)

    if is_now:
        # Remove "now" related words from text
        for pattern in now_patterns:
            text_cleaned = re.sub(pattern, '', text_cleaned, flags=re.IGNORECASE)
            logger.debug(f"Text after removing 'now' pattern '{pattern}': '{text_cleaned}'")
        event_datetime = now
    else:
        # Extract dates and times for non-"now" cases
        date_times = search_dates(text_cleaned, languages=['en'], settings={
            'PREFER_DATES_FROM': 'future',
            'RELATIVE_BASE': now
        })
        logger.debug(f"Date times found: {date_times}")

        if date_times:
            # Filter future dates
            future_dates = [(dt_text, dt) for dt_text, dt in date_times if dt > now]
            if future_dates:
                dt_text, event_datetime = future_dates[0]
                logger.debug(f"Selected future date: '{dt_text}' -> {event_datetime}")
                if len(future_dates) > 1:
                    ambiguous = True  # Multiple future dates found
                    logger.debug("Multiple future dates found; marking as ambiguous.")
            else:
                dt_text, event_datetime = date_times[0]
                ambiguous = True  # All dates are in the past
                logger.debug(f"All dates are in the past; selected date: '{dt_text}' -> {event_datetime}")
            # Remove the date/time text
            text_cleaned = re.sub(re.escape(dt_text), '', text_cleaned, flags=re.IGNORECASE)
            logger.debug(f"Text after removing date/time '{dt_text}': '{text_cleaned}'")
        else:
            event_datetime = now  # Default to now if no date/time found
            ambiguous = True
            logger.debug("No date/time found; defaulting to current time and marking as ambiguous.")

    # Clean up the event description
    event_description = re.sub(r'\s+', ' ', text_cleaned).strip().strip('.,')
    logger.debug(f"Event description after cleanup: '{event_description}'")

    # Additional cleanup for common artifacts
    event_description = re.sub(r'\b(set|schedule)\b', '', event_description, flags=re.IGNORECASE)
    event_description = re.sub(r'\s+', ' ', event_description).strip()
    logger.debug(f"Event description after additional cleanup: '{event_description}'")

    # Mark as ambiguous if description is too short
    if not event_description or len(event_description.split()) < 2:
        ambiguous = True
        logger.debug("Event description is too short; marking as ambiguous.")

    logger.debug(f"Parsed event: '{event_description}' at {event_datetime} for {duration_minutes} minutes (ambiguous: {ambiguous})")

    return event_description, event_datetime, duration_minutes, ambiguous

# Function to create an event
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
            trigger=DateTrigger(run_date=reminder_time),
            args=[update.effective_chat.id, event_description, event_id],
            id=event_id
        )

        # Send confirmation
        await update.message.reply_text(
            f"✅ Event '{event_description}' has been created.\n"
            f"📅 Date and Time: {event_datetime.strftime('%Y-%m-%d %H:%M')} - "
            f"{(event_datetime + timedelta(minutes=duration_minutes)).strftime('%H:%M')}\n"
            f"Duration: {duration_minutes} minutes"
        )

    except Exception as e:
        logger.error(f"Error creating event: {e}")
        await update.message.reply_text("❌ An error occurred while creating the event.")

# Handler for /setevent
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
            f"📝 Description: {context.user_data['pending_event']['description']}\n"
            f"📅 Date and Time: {context.user_data['pending_event']['datetime'].strftime('%Y-%m-%d %H:%M')}\n"
            f"⏰ Duration: {context.user_data['pending_event']['duration']} minutes\n"
            f"\nReply with 'yes' to confirm or 'no' to cancel."
        )
        return CONFIRMATION

    except Exception as e:
        logger.error(f"Error in set_event: {e}")
        await update.message.reply_text("❌ An error occurred while processing your request. Please try again.")
        return ConversationHandler.END

# Handler for confirmation
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
        auto_update_time = datetime.now(pytz.timezone('Asia/Kuala_Lumpur')) + timedelta(hours=1)
        scheduler.add_job(
            auto_update_event_status,
            trigger=DateTrigger(run_date=auto_update_time),
            args=[event_id],
            id=f"auto_update_{event_id}"
        )
    except Exception as e:
        logger.error(f"Error sending event check: {e}")

# Function to auto-update event status
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

# Callback handler for event response
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
            range=f'Events!B{row_number}',
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

# Command: /eventtoday
async def event_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View today's events."""
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

# Register all handlers
def register_handlers(app):
    # Command handlers
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('eventtoday', event_today))

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
    app.add_handler(conv_handler)

    # Callback query handlers
    app.add_handler(CallbackQueryHandler(handle_event_response, pattern='^event_'))

    # Error handler
    app.add_error_handler(error_handler)

if __name__ == '__main__':
    application = Application.builder().token(TOKEN).build()
    register_handlers(application)
    logger.info("Starting the Event Manager Bot...")
    application.run_polling()
