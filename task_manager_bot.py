# task_manager_bot.py

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
from config import get_sheets_service, SCOPES

# Initialize application as None
application = None

# Load environment variables from .env file
load_dotenv()

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO  # Change to DEBUG for development
)
logger = logging.getLogger(__name__)

# Get the bot token and spreadsheet ID from the environment variables
TOKEN = os.getenv('TASK_MANAGER_BOT_TOKEN')
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID')

if not TOKEN:
    raise ValueError("No token provided. Set the TASK_MANAGER_BOT_TOKEN environment variable.")

if not SPREADSHEET_ID:
    raise ValueError("No spreadsheet ID provided. Set the SPREADSHEET_ID environment variable.")

# Initialize APScheduler
scheduler = AsyncIOScheduler(timezone='Asia/Kuala_Lumpur')
scheduler.start()

# Define states for ConversationHandler
CONFIRMATION = 1

# Define accepted responses
YES_RESPONSES = ['yes', 'yea', 'yep', 'yeah', 'sure', 'affirmative']
NO_RESPONSES = ['no', 'nah', 'nope', 'negative']

# Command: /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store user's chat ID and send welcome message."""
    user_id = update.effective_chat.id
    logger.info(f"User {user_id} started the Task Manager Bot.")
    await update.message.reply_text(
        "👋 Hi! I'm your Task Manager Bot.\n"
        "I can help you add tasks, remind you about them, and track their completion.\n\n"
        "Use /help to see available commands."
    )

# Command: /help
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /help is issued."""
    await update.message.reply_text(
        "Here are the commands you can use:\n"
        "/settask - Add a new task\n"
        "/tasktoday - View tasks due today\n"
    )

# Error handler
async def error_handler(update, context):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    if update and update.effective_message:
        await update.effective_message.reply_text("An unexpected error occurred. Please try again later.")

# Function to parse natural language input for task details
def parse_natural_language(text):
    ambiguous = False
    local_tz = pytz.timezone('Asia/Kuala_Lumpur')
    now = datetime.now(local_tz)
    
    # Extract due date using search_dates
    date_times = search_dates(text, languages=['en'], settings={
        'PREFER_DATES_FROM': 'future',
        'RELATIVE_BASE': now
    })
    logger.debug(f"Date times found: {date_times}")

    if date_times:
        dt_text, due_date = date_times[0]
        text = re.sub(re.escape(dt_text), '', text, flags=re.IGNORECASE)
        logger.debug(f"Extracted due date: {due_date}")
    else:
        due_date = now
        ambiguous = True
        logger.debug("No due date found; defaulting to current time and marking as ambiguous.")

    # Remove duration phrases
    duration_patterns = [
        r'\bfor\s+\d+\s+(hour|hours|hr|hrs|minute|minutes|min|m)\b',
        r'\blast\s+\d+\s+(hour|hours|hr|hrs|minute|minutes|min|m)\b'
    ]
    for pattern in duration_patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
        logger.debug(f"Removed duration pattern: {pattern}")

    task_description = re.sub(r'\s+', ' ', text).strip().strip('.,')
    logger.debug(f"Task description after cleanup: '{task_description}'")

    if not task_description or len(task_description.split()) < 2:
        ambiguous = True
        logger.debug("Task description is too short; marking as ambiguous.")

    return task_description, due_date, ambiguous

# Function to create a task
async def create_task(update, context, task_description, due_date):
    local_tz = pytz.timezone('Asia/Kuala_Lumpur')
    if due_date.tzinfo is None:
        due_date = local_tz.localize(due_date)
    else:
        due_date = due_date.astimezone(local_tz)
    start_date = datetime.now(local_tz)

    try:
        # Log the task in Google Sheets
        sheets_service = get_sheets_service()
        values = [[
            task_description,                                      # Column A: Task Description
            'Pending',                                             # Column B: Status
            start_date.strftime('%Y-%m-%d %H:%M'),                # Column C: Start Date
            due_date.strftime('%Y-%m-%d %H:%M')                   # Column D: Due Date
        ]]

        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range='Tasks!A:D',                                     # Updated range to include only necessary columns
            valueInputOption='RAW',
            body={'values': values}
        ).execute()

        # Schedule a reminder before the due date
        reminder_time = due_date - timedelta(minutes=30)  # 30 minutes before
        scheduler.add_job(
            send_task_reminder,
            trigger=DateTrigger(run_date=reminder_time),
            args=[context.bot, update.effective_chat.id, task_description, due_date],
            id=f"task_reminder_{task_description}_{due_date.strftime('%Y%m%d%H%M')}"
        )

        # Send confirmation
        await update.message.reply_text(
            f"✅ Task '{task_description}' has been added with a due date of {due_date.strftime('%Y-%m-%d %H:%M')}."
        )

    except Exception as e:
        logger.error(f"Error creating task: {e}")
        await update.message.reply_text("❌ An error occurred while creating the task.")

# Function to send task reminder
async def send_task_reminder(bot, chat_id, task_description, due_date):
    keyboard = [
        [
            InlineKeyboardButton("✅ Completed", callback_data=f"task_done|{task_description}|{due_date.strftime('%Y-%m-%d %H:%M')}"),
            InlineKeyboardButton("❌ Not Yet", callback_data=f"task_not_done|{task_description}|{due_date.strftime('%Y-%m-%d %H:%M')}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"⏰ Reminder: Task '{task_description}' is due at {due_date.strftime('%H:%M')}. Have you completed it?",
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Error sending task reminder: {e}")

# Handler for /settask
async def set_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    command_removed = text.partition(' ')[2]

    if not command_removed:
        await update.message.reply_text(
            "Please enter the task details in either format:\n"
            "1. Natural language: /settask Go to Enoch house today at 7:00pm\n"
            "2. Structured format: /settask [Task Description] | [Due Date YYYY-MM-DD HH:MM]\n"
            "\nExample: /settask Finish report | 2024-10-25 17:00"
        )
        return ConversationHandler.END

    pattern = r'^(.+?)\s*\|\s*(.+?)\s*$'
    match = re.match(pattern, command_removed, re.IGNORECASE)

    try:
        if match:
            task_description = match.group(1).strip()
            due_date_str = match.group(2).strip()
            # Validate the due_date_str format (YYYY-MM-DD HH:MM)
            if not re.match(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}', due_date_str):
                await update.message.reply_text("❌ Please ensure the due date is in the format YYYY-MM-DD HH:MM.")
                return ConversationHandler.END

            due_date = dateparser.parse(due_date_str, settings={'PREFER_DATES_FROM': 'future'})

            if not due_date:
                await update.message.reply_text("❌ Could not parse the due date and time. Please ensure it's in a recognizable format.")
                return ConversationHandler.END

            context.user_data['pending_task'] = {
                'description': task_description,
                'due_date': due_date
            }
        else:
            task_description, due_date, ambiguous = parse_natural_language(command_removed)

            if not task_description or not due_date:
                await update.message.reply_text(
                    "❓ I couldn't understand your request. Please provide a task description and a due date."
                )
                return ConversationHandler.END

            if ambiguous:
                await update.message.reply_text(
                    "⚠️ Your input is ambiguous. Please provide more specific details about the task."
                )
                return ConversationHandler.END

            context.user_data['pending_task'] = {
                'description': task_description,
                'due_date': due_date
            }

        await update.message.reply_text(
            f"Please confirm the task details:\n"
            f"📝 Description: {context.user_data['pending_task']['description']}\n"
            f"📅 Due Date: {context.user_data['pending_task']['due_date'].strftime('%Y-%m-%d %H:%M')}\n"
            f"\nReply with 'yes' to confirm or 'no' to cancel."
        )
        return CONFIRMATION

    except Exception as e:
        logger.error(f"Error in set_task: {e}")
        await update.message.reply_text("❌ An error occurred while processing your request. Please try again.")
        return ConversationHandler.END

# Handler for confirmation
async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_response = update.message.text.strip().lower()

        if user_response in YES_RESPONSES:
            task_description = context.user_data['pending_task']['description']
            due_date = context.user_data['pending_task']['due_date']
            await create_task(update, context, task_description, due_date)
        elif user_response in NO_RESPONSES:
            await update.message.reply_text("🛑 Task creation cancelled.")
        else:
            await update.message.reply_text("❓ Invalid response. Please reply with 'yes' or 'no'.")
            return CONFIRMATION
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error handling confirmation: {e}")
        await update.message.reply_text("❌ An error occurred while handling the confirmation. Please try again.")
        return ConversationHandler.END

# Callback handler for task completion
async def handle_task_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('|')
    if len(data) != 3:
        await query.edit_message_text("❌ Invalid response.")
        return

    status, task_description, due_date_str = data
    sheets_service = get_sheets_service()

    # Read current data
    try:
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range='Tasks!A:D'
        ).execute()
        values = result.get('values', [])
    except Exception as e:
        logger.error(f"Error fetching data from Google Sheets: {e}")
        await query.edit_message_text("❌ An error occurred while accessing the task list.")
        return

    if not values or len(values) < 2:
        await query.edit_message_text("❌ No tasks found in the sheet.")
        return

    # Process sheet data
    headers = values[0]
    tasks = [dict(zip(headers, row)) for row in values[1:]]
    logger.debug(f"Tasks fetched: {tasks}")

    # Find the task
    row_number = None
    for idx, record in enumerate(tasks, start=2):
        if (record.get('Task Description', '').lower() == task_description.lower() and
                record.get('Due Date', '') == due_date_str):
            row_number = idx
            break

    if not row_number:
        await query.edit_message_text("❌ Task not found in the sheet.")
        return

    # Update status
    new_status = 'Done' if status == "task_done" else 'Pending'
    try:
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f'Tasks!B{row_number}',
            valueInputOption='RAW',
            body={'values': [[new_status]]}
        ).execute()

        # Send confirmation
        emoji = "✅" if status == "task_done" else "⏳"
        await query.edit_message_text(
            f"{emoji} Task '{task_description}' marked as {new_status}!"
        )
    except Exception as e:
        logger.error(f"Error updating task status: {e}")
        await query.edit_message_text("❌ An error occurred while updating the task status.")

# Command: /tasktoday
async def task_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View today's tasks."""
    sheets_service = get_sheets_service()
    try:
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range='Tasks!A:D'
        ).execute()
        values = result.get('values', [])

        if not values or len(values) < 2:
            await update.message.reply_text("🎉 You have no tasks for today! Great job!")
            return

        headers = values[0]
        tasks = [dict(zip(headers, row)) for row in values[1:]]
        logger.debug(f"Tasks fetched: {tasks}")

        local_tz = pytz.timezone('Asia/Kuala_Lumpur')
        now = datetime.now(local_tz)
        today_str = now.strftime('%Y-%m-%d')
        logger.debug(f"Today's date: {today_str}")

        todays_tasks = []
        for task in tasks:
            due_date_str = task.get('Due Date', '')
            if not due_date_str:
                continue
            try:
                due_date = datetime.strptime(due_date_str, '%Y-%m-%d %H:%M')
                due_date = local_tz.localize(due_date)
            except ValueError as ve:
                logger.error(f"Error parsing due date '{due_date_str}': {ve}")
                continue
            if due_date.strftime('%Y-%m-%d') == today_str and task.get('Status', '').lower() != 'done':
                todays_tasks.append(task)

        if not todays_tasks:
            await update.message.reply_text("🎉 You have no tasks for today! Great job!")
            return

        # Sort tasks by due time
        todays_tasks.sort(key=lambda x: x['Due Date'])

        message = "📝 **Today's Tasks:**\n"
        for idx, task in enumerate(todays_tasks, start=1):
            message += f"{idx}. {task.get('Task Description', 'No Description')} (Due: {task.get('Due Date', 'No Due Date')})\n"

        await update.message.reply_text(message)

    except Exception as e:
        logger.error(f"Error in /tasktoday: {e}")
        await update.message.reply_text("An unexpected error occurred. Please try again later.")

# Register all handlers
def register_handlers(app):
    # Command handlers
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('tasktoday', task_today))

    # Conversation handler for /settask
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('settask', set_task)],
        states={
            CONFIRMATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_confirmation)],
        },
        fallbacks=[CommandHandler('cancel', lambda u, c: ConversationHandler.END)],
        name="task_conversation",
        persistent=False
    )
    app.add_handler(conv_handler)

    # Callback query handlers
    app.add_handler(CallbackQueryHandler(handle_task_response, pattern='^task_'))

    # Error handler
    app.add_error_handler(error_handler)

if __name__ == '__main__':
    # Initialize the application
    application = Application.builder().token(TOKEN).build()

    # Register handlers
    register_handlers(application)

    logger.info("Starting the Task Manager Bot...")
    application.run_polling()
