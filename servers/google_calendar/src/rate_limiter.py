import os
import time
import redis.asyncio as redis
import logging
from fastapi import HTTPException

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("logs/orion.log"), logging.StreamHandler()]
)
logger = logging.getLogger("rate_limiter")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# Google API Quota limits
# Standard limit is usually something like 250 quota units per second per user, 
# but for MCP we will enforce a strict 10 requests per 10 seconds per IP or global.
RATE_LIMIT = 10
WINDOW_SECONDS = 10

async def check_rate_limit(client_id: str = "global"):
    """
    Implements a simple leaky bucket/sliding window using Redis.
    Raises an Exception if quota is exceeded.
    """
    key = f"rate_limit:{client_id}"
    current_time = int(time.time())
    logger.debug(f"Executing rate limit block calculation for client: {client_id}")
    
    async with redis_client.pipeline(transaction=True) as pipe:
        # Remove old requests
        await pipe.zremrangebyscore(key, 0, current_time - WINDOW_SECONDS)
        # Count requests in window
        await pipe.zcard(key)
        # Add current request
        await pipe.zadd(key, {str(current_time) + "_" + str(time.time_ns()): current_time})
        # Set expire so it cleans up when idle
        await pipe.expire(key, WINDOW_SECONDS)
        
        results = await pipe.execute()
        
    request_count = results[1]
    
    if request_count >= RATE_LIMIT:
        logger.error(f"Rate limit exceeded structurally! Detected {request_count} requests in {WINDOW_SECONDS}s.")
        raise Exception(f"Rate limit exceeded: {request_count} requests in the last {WINDOW_SECONDS} seconds.")
    
    logger.debug(f"Rate limit verified cleanly. Current rolling pipeline count: {request_count}/{RATE_LIMIT}")
    return True
