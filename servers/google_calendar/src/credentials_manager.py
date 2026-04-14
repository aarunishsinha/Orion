import os
import json
import redis.asyncio as redis
import logging
from google.oauth2.credentials import Credentials

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("logs/orion.log"), logging.StreamHandler()]
)
logger = logging.getLogger("credentials_manager")
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

SCOPES = ['https://www.googleapis.com/auth/calendar']
TOKEN_KEY = "gcal:token"

async def get_credentials() -> Credentials:
    """
    Retrieves the OAuth2 credentials from Redis.
    If they don't exist, it requires a local auth flow.
    If they are expired, it refreshes them and saves back to Redis.
    """
    logger.debug(f"Querying Redis for existing key: {TOKEN_KEY}")
    token_data = await redis_client.get(TOKEN_KEY)
    creds = None
    
    if token_data:
        logger.debug("Parsing JSON token data from Redis.")
        creds_dict = json.loads(token_data)
        creds = Credentials.from_authorized_user_info(creds_dict, SCOPES)

    if not creds or not creds.valid:
        logger.info("Credentials missing or expired. Evaluating auth protocol...")
        if creds and creds.expired and creds.refresh_token:
            logger.info("Token expired. Automatically refreshing via Google OAuth...")
            creds.refresh(Request())
            # Save the refreshed token back to Redis
            logger.info("Refresh successful. Pushing new token payload to Redis cache.")
            await redis_client.set(TOKEN_KEY, creds.to_json())
        else:
            logger.warning("No valid refresh token discovered. Full OAuth flow required.")
            # Need to authenticate. 
            # In a container environment, this might block. Provide link.
            if not os.path.exists("credentials.json"):
                logger.error("credentials.json file entirely missing. Halting flow.")
                raise Exception("credentials.json not found. Please download from Google Cloud Console.")
            
            logger.info("Initializing InstalledAppFlow for first-time interactive login...")
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            
            # Use console for first time auth
            logger.warning("Opening local server browser for authentication!!!")
            creds = flow.run_local_server(port=0)
            
            # Save the new token to Redis
            logger.info("Authentication complete. Syncing new token to Redis.")
            await redis_client.set(TOKEN_KEY, creds.to_json())

    return creds
