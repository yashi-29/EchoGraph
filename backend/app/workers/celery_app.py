from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery("digital_twin", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.task_default_queue = "default"
celery_app.conf.broker_connection_retry_on_startup = True
celery_app.conf.beat_schedule = {
    "poll-connectors-every-15-minutes": {
        "task": "poll_connectors",
        "schedule": 15 * 60,
    },
    # Daily sweep; each user only gets a batch once their last one is a week old.
    "generate-due-reflections-daily": {
        "task": "generate_due_reflections",
        "schedule": 24 * 60 * 60,
    },
}


@celery_app.task(name="ping")
def ping() -> str:
    """Smoke-test task proving the worker/broker wiring works end to end."""
    return "pong"


# Imported last so their tasks (poll_connectors, generate_due_reflections) register on the app.
from app.workers.tasks import connector_sync, reflections  # noqa: E402,F401
