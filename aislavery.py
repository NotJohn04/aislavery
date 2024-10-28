import os
from dotenv import load_dotenv
import logging
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)
import dateparser
from dateparser.search import search_dates
from datetime import datetime
import pytz
from typing import Optional

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Load environment variables from .env file
load_dotenv()

# Enable logging (Set to DEBUG for detailed logs)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO  # Change to DEBUG for more detailed logs
)
logger = logging.getLogger(__name__)

# Get the bot token and spreadsheet ID from the environment variables
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')

if not TOKEN:
    raise ValueError("No token provided. Set the TELEGRAM_BOT_TOKEN environment variable.")

# Define the scopes for Google APIs
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets'
]

# Initialize APScheduler (if needed for future use)
# scheduler = AsyncIOScheduler(timezone='Asia/Kuala_Lumpur')
# scheduler.start()

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

def get_sheets_service():
    creds = get_credentials()
    service = build('sheets', 'v4', credentials=creds)
    return service

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
        "I'll help you track your tasks!\n\n"
        "Use /settask to add a new task."
    )

# Error handler
async def error_handler(update, context):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    await update.message.reply_text("An unexpected error occurred. Please try again later.")

# Helper function to parse task input
def parse_task_input(text):
    """
    Parse natural language input for task details.
    Returns: (task_description, start_date, due_date, duration_days, ambiguous)
    """
    ambiguous = False
    now = datetime.now()

    # Use dateparser to extract dates
    date_results = search_dates(text, languages=['en'], settings={'PREFER_DATES_FROM': 'future'})
    dates = []
    if date_results:
        for date_str, date_obj in date_results:
            dates.append((date_str, date_obj))

    # Remove date strings from text to get task description
    task_description = text
    for date_str, _ in dates:
        task_description = task_description.replace(date_str, '')
    task_description = task_description.strip()

    # Handle different cases for dates
    start_date = None
    due_date = None
    duration_days = None

    if len(dates) == 1:
        # Only due date is provided
        due_date = dates[0][1]
        start_date = now
    elif len(dates) >= 2:
        # Both start and due dates are provided
        start_date = dates[0][1]
        due_date = dates[1][1]
    else:
        # No dates provided
        ambiguous = True

    # Calculate duration in days
    if start_date and due_date:
        duration_days = (due_date.date() - start_date.date()).days
        if duration_days < 0:
            ambiguous = True

    return task_description, start_date, due_date, duration_days, ambiguous

# Define conversation state
CONFIRM_TASK = range(1)

# Command: /settask
async def set_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.partition(' ')[2]  # Get the text after the command
    if not text:
        await update.message.reply_text(
            "Please enter the task details in natural language.\n"
            "For example: /settask Complete the website by November 22nd"
        )
        return

    # Parse the task input
    task_description, start_date, due_date, duration_days, ambiguous = parse_task_input(text)

    if ambiguous or not task_description or not due_date:
        await update.message.reply_text(
            "❓ I couldn't understand your task details. Please provide a task description and a due date.\n"
            "For example: /settask Complete the website by November 22nd"
        )
        return

    # Confirm task details with the user
    confirmation_message = (
        f"Please confirm the task details:\n"
        f"**Task Description**: {task_description}\n"
        f"**Start Date**: {start_date.strftime('%Y-%m-%d') if start_date else 'Today'}\n"
        f"**Due Date**: {due_date.strftime('%Y-%m-%d')}\n"
        f"**Duration**: {duration_days} days\n\n"
        "Reply with 'yes' to confirm or 'no' to cancel."
    )

    context.user_data['pending_task'] = {
        'description': task_description,
        'start_date': start_date,
        'due_date': due_date,
        'duration_days': duration_days
    }

    await update.message.reply_text(confirmation_message, parse_mode='Markdown')

    return CONFIRM_TASK

# Handler for task confirmation
async def handle_task_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_response = update.message.text.strip().lower()

    if user_response not in ['yes', 'no']:
        await update.message.reply_text("❓ Please reply with 'yes' to confirm or 'no' to cancel.")
        return CONFIRM_TASK

    if user_response == 'no':
        await update.message.reply_text("🛑 Task creation cancelled.")
        context.user_data.pop('pending_task', None)
        return ConversationHandler.END

    pending_task = context.user_data.get('pending_task')
    if not pending_task:
        await update.message.reply_text("⚠️ No pending task found. Please start over with /settask.")
        return ConversationHandler.END

    # Proceed to create the task
    await create_task(update, context, pending_task)
    context.user_data.pop('pending_task', None)
    return ConversationHandler.END

# Function to create task and insert into Google Sheets
async def create_task(update, context, task_info):
    try:
        task_description = task_info['description']
        start_date = task_info['start_date']
        due_date = task_info['due_date']
        duration_days = task_info['duration_days']

        # Localize dates
        local_tz = pytz.timezone('Asia/Kuala_Lumpur')  # Replace with your timezone
        if start_date:
            start_date = local_tz.localize(start_date)
        else:
            start_date = datetime.now(local_tz)
        due_date = local_tz.localize(due_date)

        # Prepare data for Google Sheets
        sheets_service = get_sheets_service()
        values = [[
            task_description,
            'Pending',
            start_date.strftime('%Y-%m-%d'),
            due_date.strftime('%Y-%m-%d'),
            duration_days
        ]]

        # Append to Google Sheets
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range='Tasks!A:E',
            valueInputOption='RAW',
            body={'values': values}
        ).execute()

        await update.message.reply_text(
            f"✅ Task '{task_description}' has been added with a due date of {due_date.strftime('%Y-%m-%d')}."
        )

    except Exception as e:
        logger.error(f"Error creating task: {e}")
        await update.message.reply_text("❌ An error occurred while adding the task.")

# Register handlers
def register_handlers(application):
    # Command handlers
    application.add_handler(CommandHandler('start', start))

    # Conversation handler for /settask
    task_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('settask', set_task)],
        states={
            CONFIRM_TASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task_confirmation)],
        },
        fallbacks=[CommandHandler('cancel', lambda u, c: ConversationHandler.END)],
        name="task_conversation",
        persistent=False
    )
    application.add_handler(task_conv_handler)

    # Error handler
    application.add_error_handler(error_handler)

# Main function to start the bot
if __name__ == '__main__':
    application = Application.builder().token(TOKEN).build()
    register_handlers(application)
    logger.info("Bot started successfully")
    application.run_polling()
