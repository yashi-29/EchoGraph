from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.graph import GraphResponse
from app.services.graph.build import get_graph

router = APIRouter()


@router.get("/", response_model=GraphResponse)
async def knowledge_graph(
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """People, organizations and topics across the user's memories, and how they connect.
    Built from stored data (services/graph/) and cached until the memories change."""
    return await get_graph(current_user, db)
