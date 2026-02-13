"""Async event bus for inter-component communication."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Callable, Coroutine

import structlog

logger = structlog.get_logger()

EventHandler = Callable[..., Coroutine[Any, Any, None]]


class EventBus:
    """Simple async event bus for decoupled component communication."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)
        logger.debug("event_subscribed", event_type=event_type, handler=handler.__qualname__)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers[event_type].remove(handler)

    async def emit(self, event_type: str, **kwargs: Any) -> None:
        handlers = self._handlers.get(event_type, [])
        if not handlers:
            return
        tasks = [asyncio.create_task(h(**kwargs)) for h in handlers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error(
                    "event_handler_error",
                    event_type=event_type,
                    error=str(result),
                )
