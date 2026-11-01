import asyncio
import logging

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.connector import Connector
from app.services.ingestion.sync import sync_connector
from app.services.oauth.tokens import ConnectorAccessExpired
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="poll_connectors")
def poll_connectors() -> dict[str, int | str]:
    return asyncio.run(_poll_connectors_async())


async def _poll_connectors_async() -> dict[str, int | str]:
    async with async_session_factory() as db:
        connector_ids = (await db.execute(select(Connector.id))).scalars().all()

    results: dict[str, int | str] = {}
    # Each connector gets its own session/transaction — one connector's bad
    # token or a mid-sync error must not affect any other connector's sync,
    # and re-using one session across a failure risks leaving it unusable.
    for connector_id in connector_ids:
        async with async_session_factory() as db:
            connector = await db.get(Connector, connector_id)
            if connector is None:
                continue
            key = f"{connector.user_id}:{connector.provider}"
            try:
                results[key] = await sync_connector(connector, db)
            except ConnectorAccessExpired:
                # Retrying can't help until the user reconnects — one line, not a traceback.
                logger.warning("Skipping %s: Google access expired, needs reconnect", key)
                results[key] = "needs_reconnect"
            except Exception:
                logger.exception("poll_connectors failed for %s", key)
                results[key] = "error"
    return results
