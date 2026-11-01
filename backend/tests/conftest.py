import asyncio
import json
import sys
from types import SimpleNamespace

if sys.platform == "win32":
    # psycopg's async mode cannot run on Windows' default ProactorEventLoop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def fake_tool_call_response(name: str, arguments: dict) -> object:
    """A fake Groq ChatCompletion whose message is a single tool call."""
    tool_call = SimpleNamespace(
        id=f"call_{name}", function=SimpleNamespace(name=name, arguments=json.dumps(arguments))
    )
    message = SimpleNamespace(content=None, tool_calls=[tool_call])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def fake_text_response(text: str) -> object:
    """A fake Groq ChatCompletion whose message is plain text (no tool call)."""
    message = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def fake_groq_client(responses: list[object]) -> object:
    """A fake AsyncGroq client that returns `responses` in order, one per .create() call."""

    async def fake_create(**_kwargs: object) -> object:
        return responses.pop(0)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
