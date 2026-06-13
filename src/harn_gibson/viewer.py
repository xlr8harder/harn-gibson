"""Shared local browser viewer lifecycle."""

from __future__ import annotations

import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ViewerHandle:
    server: Any
    state: Any
    thread: threading.Thread
    display_url: str
    endpoint: str
    input_endpoint: str
    browser_open: Callable[[str], Any]
    closed: bool = False

    def open_browser(self) -> None:
        self.browser_open(self.display_url)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        pipeline = getattr(self.state, "pipeline", None)
        stop = getattr(pipeline, "stop", None)
        if callable(stop):
            stop()
        self.server.shutdown()
        self.server.server_close()


def start_viewer(
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    state: Any | None = None,
    env: dict[str, str] | None = None,
    launch_browser: bool = True,
    browser_open: Callable[[str], Any] | None = None,
) -> ViewerHandle:
    from harn_gibson.server import build_state_from_env, create_server

    resolved_state = build_state_from_env(env) if state is None else state
    server = create_server(host, port, resolved_state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    actual_host, actual_port = server.server_address
    display_url = f"http://{actual_host}:{actual_port}"
    open_browser = webbrowser.open if browser_open is None else browser_open
    handle = ViewerHandle(
        server=server,
        state=resolved_state,
        thread=thread,
        display_url=display_url,
        endpoint=f"{display_url}/events",
        input_endpoint=f"{display_url}/input/next",
        browser_open=open_browser,
    )
    if launch_browser:
        handle.open_browser()
    return handle
