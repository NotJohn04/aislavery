# config.py

import os
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import logging

# Define the scopes for Google APIs
SCOPES = [
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/spreadsheets'
]

def get_credentials():
    """Get and refresh Google OAuth2 credentials."""
    creds = None
    if os.path.exists('token.json'):
        try:
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
            logging.info("Loaded existing credentials from token.json")
        except Exception as e:
            logging.error(f"Error loading token.json: {e}")
            os.remove('token.json')
            logging.info("Removed invalid token.json")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logging.info("Refreshing expired credentials")
            creds.refresh(Request())
        else:
            if not os.path.exists('credentials.json'):
                raise FileNotFoundError("credentials.json not found")

            logging.info("Initiating new OAuth flow")
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json',
                SCOPES
            )
            creds = flow.run_local_server(
                port=0,
                access_type='offline',
                prompt='consent'
            )

        logging.info("Saving new credentials to token.json")
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    return creds

def get_sheets_service():
    creds = get_credentials()
    service = build('sheets', 'v4', credentials=creds)
    return service

def get_calendar_service():
    creds = get_credentials()
    service = build('calendar', 'v3', credentials=creds)
    return service
