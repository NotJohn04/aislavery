import dateparser
from dateparser.search import search_dates

print(f"dateparser version: {dateparser.__version__}")

texts = [
    "set appointment with teacher at 430 later today",
    "Prepare slides for the team meeting on Monday at 10 AM for 2 hours."
]

for text in texts:
    results = search_dates(
        text,
        languages=['en'],
        settings={
            'PREFER_DATES_FROM': 'future',
            'RETURN_AS_TIMEZONE_AWARE': False
        }
    )
    print(f"Input: {text}\nParsed Results: {results}\n")
