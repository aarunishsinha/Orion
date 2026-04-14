import asyncio
import os
import datetime
import logging
from arq.connections import RedisSettings
from googleapiclient.discovery import build
from src.credentials_manager import get_credentials
from src.rate_limiter import check_rate_limit

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("logs/orion.log"), logging.StreamHandler()]
)
logger = logging.getLogger("redis_worker")

# Worker needs to know about Redis
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

async def _get_service():
    """Helper to initialize the calendar service."""
    logger.debug("Fetching OAuth credentials to build Google Calendar service...")
    creds = await get_credentials()
    if not creds:
        logger.error("Credentials not found! Please download from Google Cloud Console.")
        raise Exception("Operation failed: credentials.json not found. Please download from Google Cloud Console.")
    
    logger.info("Initializing Google Calendar v3 service...")
    return await asyncio.to_thread(build, 'calendar', 'v3', credentials=creds)

async def check_api_quota():
    logger.debug("Checking leaky bucket quota for 'mcp_worker'")
    if not await check_rate_limit("mcp_worker"):
        logger.warning("Rate limit exceeded! Rejecting outgoing API execution.")
        raise Exception("Rate limit exceeded. Please try again later.")

async def _to_thread(func, *args, **kwargs):
    """Helper to run synchronous Google API client calls in a thread."""
    return await asyncio.to_thread(func, *args, **kwargs)

async def list_upcoming_events(ctx, max_results: int = 5):
    logger.info(f"Worker processing: list_upcoming_events (max_results={max_results})")
    await check_api_quota()
    service = await _get_service()
    
    now = datetime.datetime.utcnow().isoformat() + 'Z'  # 'Z' indicates UTC time
    
    def _execute():
        events_result = service.events().list(
            calendarId='primary', timeMin=now,
            maxResults=max_results, singleEvents=True,
            orderBy='startTime').execute()
        return events_result.get('items', [])
        
    events = await _to_thread(_execute)
    
    # Format the return just nicely for the LLM
    result = []
    for event in events:
        start = event['start'].get('dateTime', event['start'].get('date'))
        result.append({
            "id": event["id"],
            "summary": event.get("summary", "No Title"),
            "start": start,
            "htmlLink": event.get("htmlLink", "")
        })
    return result

async def create_event(ctx, summary: str, start_time: str, end_time: str, description: str = ""):
    logger.info(f"Worker processing: create_event (Summary: {summary})")
    await check_api_quota()
    service = await _get_service()
    
    event_body = {
        'summary': summary,
        'description': description,
        'start': {'dateTime': start_time},
        'end': {'dateTime': end_time},
    }
    
    def _execute():
        return service.events().insert(calendarId='primary', body=event_body).execute()
        
    event = await _to_thread(_execute)
    return {"status": "success", "event_id": event.get('id'), "link": event.get('htmlLink')}

async def update_event(ctx, event_id: str, summary: str = None, start_time: str = None, end_time: str = None):
    logger.info(f"Worker processing: update_event (Event ID: {event_id})")
    await check_api_quota()
    service = await _get_service()
    
    def _get_event():
        return service.events().get(calendarId='primary', eventId=event_id).execute()
        
    event = await _to_thread(_get_event)
    
    if summary:
        event['summary'] = summary
    if start_time:
        event['start']['dateTime'] = start_time
    if end_time:
        event['end']['dateTime'] = end_time
        
    def _update():
        return service.events().update(calendarId='primary', eventId=event_id, body=event).execute()
        
    updated_event = await _to_thread(_update)
    return {"status": "success", "event_id": updated_event.get('id'), "link": updated_event.get('htmlLink')}

async def check_conflicts(ctx, start_time: str, end_time: str):
    logger.info(f"Worker processing: check_conflicts (From: {start_time} - To: {end_time})")
    await check_api_quota()
    service = await _get_service()
    
    def _execute():
        # Use freebusy query to check conflicts exactly
        body = {
            "timeMin": start_time,
            "timeMax": end_time,
            "items": [{"id": "primary"}]
        }
        return service.freebusy().query(body=body).execute()
        
    result = await _to_thread(_execute)
    calendars = result.get('calendars', {})
    primary = calendars.get('primary', {})
    busy_slots = primary.get('busy', [])
    
    if len(busy_slots) > 0:
        return {"conflict": True, "busy_slots": busy_slots}
    return {"conflict": False}

# ARQ worker settings
class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
    functions = [
        list_upcoming_events,
        create_event,
        update_event,
        check_conflicts
    ]
