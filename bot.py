import os
from dotenv import load_dotenv
import logging
from telegram.ext import Application, CommandHandler, MessageHandler, filters
import dateparser
from dateparser.search import search_dates  # New import
from datetime import timedelta
import re
import spacy

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import pytz

# Load environment variables from .env file
load_dotenv()

# Enable logging (Set to DEBUG for detailed logs)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.DEBUG
)

logger = logging.getLogger(__name__)

# Get the bot token from the environment variable
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

if not TOKEN:
    raise ValueError("No token provided. Set the TELEGRAM_BOT_TOKEN environment variable.")

# Define the scope for Google Calendar
SCOPES = ['https://www.googleapis.com/auth/calendar.events']

# Initialize spaCy's English model
nlp = spacy.load("en_core_web_sm")

def get_calendar_service():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
        creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    service = build('calendar', 'v3', credentials=creds)
    return service

async def start(update, context):
    await update.message.reply_text('Hi! Send me a message with your task details.')

async def extract_task_details(text):
    # Define your local time zone
    local_tz = pytz.timezone('Asia/Kuala_Lumpur')  # Replace with your actual time zone

    # Use search_dates to find all date/time expressions
    results = search_dates(
        text,
        languages=['en'],
        settings={
            'PREFER_DATES_FROM': 'future',
            'RETURN_AS_TIMEZONE_AWARE': False
        }
    )

    if not results:
        logging.warning(f"No dates found in text: '{text}'")
        return None

    # Initialize variables
    date_time = None
    duration = timedelta(hours=1)  # Default duration
    task_title_cleaned = text

    # Extract date_time and duration
    for match_text, match_dt in results:
        # Identify if the matched text relates to time or duration
        if re.search(r'\b(?:at|on|today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', match_text, re.IGNORECASE):
            if not date_time:
                date_time = match_dt
                # Adjust AM/PM based on context
                if re.search(r'later\s+today', text, re.IGNORECASE) and date_time.hour < 12:
                    date_time += timedelta(hours=12)  # Convert to PM
        elif re.search(r'\bfor\s+\d+\s*(?:minutes?|hours?)\b', match_text, re.IGNORECASE):
            if date_time:
                # Calculate duration based on the difference
                # However, since dateparser cannot parse durations, we'll extract the number and unit
                duration_match = re.search(r'for\s+(\d+)\s*(minutes?|hours?)', match_text, re.IGNORECASE)
                if duration_match:
                    duration_value = int(duration_match.group(1))
                    duration_unit = duration_match.group(2).lower()
                    if 'hour' in duration_unit:
                        duration = timedelta(hours=duration_value)
                    else:
                        duration = timedelta(minutes=duration_value)

    if not date_time:
        logging.warning(f"Date parsing failed for text: '{text}'")
        return None

    logging.debug(f"Extracted DateTime: {date_time}")
    logging.debug(f"Extracted Duration: {duration}")

    # Remove all date/time expressions from text to extract task title
    for match_text, _ in results:
        task_title_cleaned = task_title_cleaned.replace(match_text, '')

    logging.debug(f"Text after removing date/time expressions: '{task_title_cleaned}'")

    # Further clean up using regex to remove unwanted phrases
    task_title_cleaned = re.sub(r'\b(set|for|at|on|later today)\b', '', task_title_cleaned, flags=re.IGNORECASE)
    logging.debug(f"Text after removing unwanted phrases: '{task_title_cleaned}'")

    # Use spaCy to parse the cleaned text
    doc = nlp(task_title_cleaned)

    # Extract noun chunks for better task title accuracy
    noun_chunks = list(doc.noun_chunks)
    if noun_chunks:
        task_title = ' '.join([chunk.text for chunk in noun_chunks]).strip().capitalize()
        logging.debug(f"Extracted Task Title from noun chunks: '{task_title}'")
    else:
        # Fallback to token-based extraction
        task_title_tokens = [token.text for token in doc if not token.is_stop and not token.is_punct]
        task_title = ' '.join(task_title_tokens).strip().capitalize()
        logging.debug(f"Extracted Task Title from tokens: '{task_title}'")

    if not task_title:
        logging.warning(f"Task title extraction failed for text: '{text}'")
        return None

    # Log extracted details for debugging
    logging.info(f"Extracted Task Details: Title='{task_title}', DateTime='{date_time}', Duration='{duration}'")

    return {
        'title': task_title,
        'date_time': date_time,
        'duration': duration
    }

async def process_text(text, update, context):
    task_details = await extract_task_details(text)
    if task_details:
        # Proceed to create event in Google Calendar
        await create_calendar_event(task_details, update)
    else:
        await update.message.reply_text('Sorry, I could not extract task details from your message. Please try again.')

async def create_calendar_event(task_details, update):
    try:
        service = get_calendar_service()
        date_time = task_details['date_time']
        duration = task_details['duration']

        # Set your local time zone
        local_tz = pytz.timezone('Asia/Kuala_Lumpur')  # Replace with your time zone, e.g., 'America/New_York'

        # Localize the date and time
        date_time = local_tz.localize(date_time)

        event = {
            'summary': task_details['title'],
            'start': {
                'dateTime': date_time.isoformat(),
                'timeZone': str(local_tz),
            },
            'end': {
                'dateTime': (date_time + duration).isoformat(),
                'timeZone': str(local_tz),
            },
        }
        event = service.events().insert(calendarId='primary', body=event).execute()
        await update.message.reply_text(
            f"✅ Event '{task_details['title']}' has been created in your Google Calendar.\n"
            f"📅 Date and Time: {date_time.strftime('%Y-%m-%d %H:%M')} - {(date_time + duration).strftime('%H:%M')}\n"
            f"⏰ Duration: {duration}"
        )
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        await update.message.reply_text("❌ Sorry, I couldn't create the event in your calendar.")

async def handle_text(update, context):
    await process_text(update.message.text, update, context)

def main():
    application = Application.builder().token(TOKEN).build()

    # Start command handler
    application.add_handler(CommandHandler('start', start))

    # Message handler for text messages
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Start the bot
    application.run_polling()

if __name__ == '__main__':
    main()
