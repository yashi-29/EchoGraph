from fastapi import APIRouter

from app.api.v1.endpoints import (
    account,
    auth,
    chat,
    connectors,
    dashboard,
    graph,
    health,
    memory,
    reflections,
    uploads,
)

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(account.router, prefix="/account", tags=["account"])
api_router.include_router(connectors.router, prefix="/connectors", tags=["connectors"])
api_router.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"])
api_router.include_router(memory.router, prefix="/memory", tags=["memory"])
api_router.include_router(reflections.router, prefix="/reflections", tags=["reflections"])
api_router.include_router(uploads.router, prefix="/uploads", tags=["uploads"])
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
api_router.include_router(graph.router, prefix="/graph", tags=["graph"])
