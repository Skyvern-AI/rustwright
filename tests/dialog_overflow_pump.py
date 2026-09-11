"""Browser-free pump exercise shared by native and Python regression pins."""

import asyncio
import threading
import warnings

from rustwright.sync_api import Page
from rustwright.async_api import AsyncPage


def exercise_dialog_overflow_pump(stream, publish, async_mode, ownership, expected_overflows=2):
    class Core:
        def __init__(self):
            self.dialog_actions = []

        def combined_event_stream(self):
            return stream

        def keyboard_primary_modifier(self):
            return "Control"

        def is_closed(self):
            return False

        def handle_dialog(self, accept, _prompt, _timeout):
            self.dialog_actions.append(accept)

    async def run():
        core = Core()
        page = Page(core, _start_event_pump=not async_mode)
        wrapper = AsyncPage(page) if async_mode else None
        entered, release, drained, claimed = (threading.Event() for _ in range(4))
        dialogs, preceding_actions = [], []

        def on_console(message):
            if message.text == "blocked":
                entered.set()
                assert release.wait(5)
            else:
                release.clear()
                drained.set()
                assert release.wait(5)

        def claim(dialog):
            preceding_actions.append(list(core.dialog_actions))
            dialogs.append(dialog)
            claimed.set()
            return True

        async def async_listener(dialog):
            claim(dialog._sync)

        # Avoid Runtime.enable: all events come from the supplied test transport.
        page._event_handlers["console"] = [on_console]
        page._event_handler_cursors[("console", id(on_console))] = 0
        waiter = None
        if ownership == "listener":
            if wrapper is None:
                page.on("dialog", claim)
            else:
                wrapper.on("dialog", async_listener)
        elif ownership == "waiter":
            waiter = page.expect_event("dialog", predicate=claim, timeout=2000)
            waiter.__enter__()
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                publish(0)
                assert await asyncio.to_thread(entered.wait, 2)
                publish(1)
                assert core.dialog_actions == []
                release.set()
                assert await asyncio.to_thread(drained.wait, 2)
                if ownership != "unowned":
                    assert await asyncio.to_thread(claimed.wait, 2)
                    if waiter is not None:
                        waiter.__exit__(None, None, None)
                        waiter = None
                    assert preceding_actions == [[]]
                    assert core.dialog_actions == []
                else:
                    assert core.dialog_actions == [False]
                # The native dialog stays open for the owner. Another overflow
                # must not redispatch it or create an unowned fallback.
                drained.clear()
                publish(2)
                release.set()
                assert await asyncio.to_thread(drained.wait, 2)
                page._maybe_fallback_pending_dialogs()
                assert len(dialogs) == (0 if ownership == "unowned" else 1)
                assert core.dialog_actions == ([False] if ownership == "unowned" else [])
                if dialogs:
                    dispatch = dialogs[0]._dispatch
                    assert dispatch.captured if ownership == "waiter" else dispatch.handler_claims
                    await asyncio.to_thread(dialogs[0].accept)
                    assert core.dialog_actions == [True]
                assert sum("event stream overflow" in str(w.message) for w in caught) == expected_overflows
        finally:
            release.set()
            if waiter is not None:
                waiter.__exit__(RuntimeError, RuntimeError("cleanup"), None)
            page._stop_event_pump()
            if wrapper is not None:
                await asyncio.wait_for(wrapper._event_pump_task, 2)
            else:
                await asyncio.to_thread(page._event_pump_thread.join, 2)
                assert not page._event_pump_thread.is_alive()

    asyncio.run(run())
