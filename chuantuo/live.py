"""Cursor-based local SSE, coalesced at the display cadence rather than model cadence."""
import asyncio

from anyio import to_thread

from .engine import encode
from .insights import event_preview


async def stream_events(simulation, offset, request):
    known_window = None
    yield "retry: 1500\n\n"
    while not await request.is_disconnected():
        page = simulation.journal.page(offset, 160, event_preview)
        snapshot = simulation.snapshot(known_window)
        for event in page["events"]:
            event["commit_status"] = simulation.event_status(event)
        if page["events"] or known_window is None:
            offset = page["next_offset"]
            yield f"id: {offset}\nevent: batch\ndata: {encode({**page, 'snapshot':snapshot})}\n\n"
        known_window = snapshot["window"]
        if page["has_more"]:
            await asyncio.sleep(0)
            continue
        if snapshot["status"] not in {"ready","running"} and not simulation.journal.wait_for_offset(offset,0):
            yield f"event: idle\ndata: {encode({'next_offset':offset})}\n\n"
            return
        changed = await to_thread.run_sync(simulation.journal.wait_for_offset, offset, 8, abandon_on_cancel=True)
        if changed:
            await asyncio.sleep(.12)
        else:
            yield ": heartbeat\n\n"
