# app/sse.py
import json
import queue
import time
from typing import Any, Dict, Generator, Optional

class SSEBroker:
    """Minimal broker that fans out in-process Server-Sent Events."""

    def __init__(self) -> None:
        """Initialize the broker and its subscription registry."""
        self._subs: set[queue.Queue] = set()
        
    def subscribe(self) -> queue.Queue:
        """Create and register a new subscription queue.

        Returns:
            queue.Queue: Queue that yields published SSE messages.
        """
        q: queue.Queue = queue.Queue()
        self._subs.add(q)
        return q
    
    def unsubscribe(self, q: queue.Queue) -> None:
        """Remove a queue from the subscriber set.

        Args:
            q (queue.Queue): Subscription queue returned by ``subscribe``.
        """
        self._subs.discard(q)
    
    def publish(self, data: Any, event: str = "message", id: Optional[str] = None) -> None:
        """Enqueue an event for all subscribers, pruning dead queues.

        Args:
            data (Any): JSON-serializable payload.
            event (str): SSE event name, defaults to ``\"message\"``.
            id (str | None): Optional event ID forwarded to the client.
        """
        msg = {"event": event, "data": data, "id": id}
        dead = []
        
        for q in self._subs:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        
        for q in dead:
            self.unsubscribe(q)        

def _format_sse(data: Any, event: str = "message", id: Optional[str] = None) -> str:
    """Serialize a payload into SSE wire format.

    Args:
        data (Any): JSON-serializable payload or plain string.
        event (str): Event name to set in the SSE frame.
        id (str | None): Optional identifier to include.

    Returns:
        str: Multi-line SSE frame ending with a blank line.
    """
    if not isinstance(data, str):
        data = json.dumps(data)
        
        lines = []
        if id is not None:
            lines.append(f"id: {id}")
        if event:
            lines.append(f"event: {event}")
        
        for line in str(data).splitlines() or [""]:
            lines.append(f"data: {line}")
            
        return "\n".join(lines) + "\n\n"

def stream(q: queue.Queue, keepalive_seconds: int = 15) -> Generator[str, None, None]:
    """Yield SSE frames from a queue, inserting keep-alives as needed.

    Args:
        q (queue.Queue): Source queue created via ``SSEBroker.subscribe``.
        keepalive_seconds (int): Idle duration that triggers a comment frame.

    Yields:
        str: Formatted SSE frame ready to stream to the client.
    """
    last = time.time()
    while True:
        try:
            msg = q.get(timeout = 1)
            yield _format_sse(msg["data"], msg["event"], msg["id"])
        except queue.Empty:
            if time.time() - last >= keepalive_seconds:
                yield ": keep-alive\n\n"
                last = time.time()
                
