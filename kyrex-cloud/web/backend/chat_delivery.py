"""Bounded, process-local SSE delivery for accepted browser Chat turns."""
import asyncio
import time
from contextlib import aclosing


_MAX_TURNS = 48
_MAX_FRAMES = 4096
_MAX_BYTES = 2 * 1024 * 1024
_FINISHED_TTL = 5 * 60


class Delivery:
    def __init__(self, user, conversation, payload):
        self.user = user
        self.conversation = conversation
        self.payload = payload
        self.frames = {}
        self.bytes = 0
        self.cursor = 0
        self.finished_at = None
        self.changed = asyncio.Event()

    def _notify(self):
        self.changed.set()
        self.changed = asyncio.Event()

    async def produce(self, source):
        try:
            async with aclosing(source):
                async for frame in source:
                    self.cursor += 1
                    size = len(frame.encode())
                    self.frames[self.cursor] = size, frame
                    self.bytes += size
                    while len(self.frames) > 1 and (
                            self.bytes > _MAX_BYTES or len(self.frames) > _MAX_FRAMES):
                        self.bytes -= self.frames.pop(next(iter(self.frames)))[0]
                    self._notify()
        finally:
            self.finished_at = time.monotonic()
            self._notify()

    async def view(self, cursor):
        while True:
            changed = self.changed
            # A long reply may age out its first deltas. The terminal event
            # carries the complete saved answer and replaces partial text.
            while cursor < self.cursor:
                cursor = max(cursor + 1, self.cursor - len(self.frames) + 1)
                yield f"id: {cursor}\n{self.frames[cursor][1]}"
            if self.finished_at is not None and cursor >= self.cursor:
                return
            await changed.wait()


deliveries = {}
_producers = set()


def find(request_id):
    delivery = deliveries.get(request_id)
    if delivery and delivery.finished_at is not None and time.monotonic() - delivery.finished_at >= _FINISHED_TTL:
        del deliveries[request_id]
        return None
    return delivery


def start(request_id, delivery, source):
    """Admit one producer, with no await between registration and task start."""
    for key, old in tuple(deliveries.items()):
        if old.finished_at is not None and time.monotonic() - old.finished_at >= _FINISHED_TTL:
            del deliveries[key]
    if len(deliveries) >= _MAX_TURNS:
        oldest = next((key for key, old in deliveries.items() if old.finished_at is not None), None)
        if oldest is None:
            return False
        del deliveries[oldest]
    deliveries[request_id] = delivery
    task = asyncio.create_task(delivery.produce(source))
    _producers.add(task)
    task.add_done_callback(_producers.discard)
    return True
