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

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# Google API Quota limits
# Standard limit is usually something like 250 quota units per second per user, 
# but for MCP we will enforce a strict 10 requests per 10 seconds per IP or global.
RATE_LIMIT = 10
WINDOW_SECONDS = 10

# Sliding window, evaluated atomically inside Redis so the quota is global across
# worker replicas. Only admitted requests are recorded — rejected ones must not
# consume quota. Returns the in-window count (before this request) and an
# admitted flag.
_SLIDING_WINDOW_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, tonumber(ARGV[1]) - tonumber(ARGV[2]))
local count = redis.call('ZCARD', KEYS[1])
if count < tonumber(ARGV[3]) then
    redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
    redis.call('PEXPIRE', KEYS[1], ARGV[2])
    return {count, 1}
end
return {count, 0}
"""
_sliding_window = redis_client.register_script(_SLIDING_WINDOW_LUA)

async def check_rate_limit(client_id: str = "global"):
    """
    Sliding-window limiter: at most RATE_LIMIT admitted requests in any
    WINDOW_SECONDS span. Raises an Exception if quota is exceeded.
    """
    key = f"rate_limit:{client_id}"
    now_ms = int(time.time() * 1000)
    member = f"{now_ms}_{time.time_ns()}"
    logger.debug(f"Executing rate limit block calculation for client: {client_id}")

    request_count, admitted = await _sliding_window(
        keys=[key], args=[now_ms, WINDOW_SECONDS * 1000, RATE_LIMIT, member]
    )

    if not admitted:
        logger.error(f"Rate limit exceeded structurally! Detected {request_count} requests in {WINDOW_SECONDS}s.")
        raise Exception(f"Rate limit exceeded: {request_count} requests in the last {WINDOW_SECONDS} seconds.")

    logger.info(f"Rate limit admitted: {request_count + 1}/{RATE_LIMIT} in window")
    return True
