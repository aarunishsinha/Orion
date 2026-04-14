import asyncio
import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from pydantic import BaseModel, Field
from mcp.server.fastmcp import FastMCP
from arq import create_pool
from arq.connections import RedisSettings

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("logs/orion.log"), logging.StreamHandler()]
)
logger = logging.getLogger("mcp_server")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
# We will hold a global reference to the arq redis pool
redis_pool = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_pool
    logger.info(f"Connecting to Redis at {REDIS_URL} for ARQ pool...")
    redis_pool = await create_pool(RedisSettings.from_dsn(REDIS_URL))
    logger.info("Redis ARQ pool successfully initialized.")
    yield
    if redis_pool:
        logger.info("Closing Redis ARQ pool...")
        await redis_pool.close()
        logger.info("Redis ARQ pool closed.")

app = FastAPI(title="Orion Google Calendar MCP Server", lifespan=lifespan)

# Initialize FastMCP
mcp = FastMCP("Orion Calendar", dependencies=[])

async def call_worker(func_name: str, *args):
    """Enqueue job into arq and await with strict 5 second timeout"""
    logger.debug(f"Attempting to call worker function: {func_name} with args: {args}")
    if not redis_pool:
        logger.error("Redis pool not initialized before invoking call_worker.")
        raise Exception("Redis pool not initialized")
        
    job = await redis_pool.enqueue_job(func_name, *args)
    if not job:
        logger.error(f"Failed to enqueue job: {func_name}")
        raise Exception(f"Failed to enqueue job {func_name}")
        
    logger.info(f"Job {job.job_id} ({func_name}) enqueued successfully. Waiting for result...")
    try:
        # Strict 5 second timeout as requested by user
        result = await asyncio.wait_for(job.result(timeout=None), timeout=5.0)
        logger.info(f"Job {job.job_id} completed successfully within timeout limits.")
        return result
    except asyncio.TimeoutError:
        logger.warning(f"Timeout limit (5s) reached while waiting for job {job.job_id} ({func_name}).")
        return {"status": "error", "message": f"Google API timed out after 5 seconds inside {func_name}"}
    except Exception as e:
        logger.error(f"Operation failed for job {job.job_id} ({func_name}): {str(e)}")
        return {"status": "error", "message": f"Operation failed: {str(e)}"}

@mcp.tool()
async def list_upcoming_events(max_results: int = 5) -> str:
    """Fetches the next N upcoming events from Google Calendar."""
    logger.info(f"Executing MCP Tool: list_upcoming_events (max_results={max_results})")
    result = await call_worker("list_upcoming_events", max_results)
    return str(result)

@mcp.tool()
async def create_event(summary: str, start_time: str, end_time: str, description: str = "") -> str:
    """
    Creates a new Google Calendar event.
    start_time and end_time must be RFC3339 formatted strings, e.g., '2026-04-12T10:00:00Z'.
    """
    logger.info(f"Executing MCP Tool: create_event (Summary: {summary})")
    result = await call_worker("create_event", summary, start_time, end_time, description)
    return str(result)

@mcp.tool()
async def update_event(event_id: str, summary: str = None, start_time: str = None, end_time: str = None) -> str:
    """
    Updates an existing Google Calendar event.
    """
    logger.info(f"Executing MCP Tool: update_event (Event ID: {event_id})")
    result = await call_worker("update_event", event_id, summary, start_time, end_time)
    return str(result)

@mcp.tool()
async def check_conflicts(start_time: str, end_time: str) -> str:
    """
    Checks if there are any conflicting events in the specified time slot.
    start_time and end_time must be RFC3339 formatted strings.
    """
    logger.info(f"Executing MCP Tool: check_conflicts (From: {start_time} - To: {end_time})")
    result = await call_worker("check_conflicts", start_time, end_time)
    return str(result)

# Mount the generic Starlette SSE application provided by FastMCP directly into FastAPI
logger.info("Mounting FastMCP endpoints to FastAPI on /mcp...")
app.mount("/mcp", mcp.sse_app())
logger.info("FastMCP endpoints successfully mounted over SSE/HTTP structure.")
