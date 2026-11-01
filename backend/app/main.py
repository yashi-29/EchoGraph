import asyncio
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import get_settings

if sys.platform == "win32":
    # psycopg's async mode cannot run on Windows' default ProactorEventLoop
    # (only relevant when running outside the Linux-based Docker containers).
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

settings = get_settings()

app = FastAPI(title="Digital Twin API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.backend_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Lets the frontend read the export's suggested filename (Settings → Export your data).
    expose_headers=["Content-Disposition"],
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "digital-twin-api", "status": "running"}
