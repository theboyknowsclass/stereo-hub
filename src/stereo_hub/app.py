"""FastAPI service wrapping one LibreSyncClient.

One long-lived client holds both hub sockets open and keeps `state` current from
the hub's own pushes, so reads never touch the network and writes return once
the hub has confirmed them.
"""

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Any, Literal, Protocol

from aiolibresync import (
    ConfirmationTimeout,
    DeviceState,
    LibreSyncClient,
    LibreSyncError,
    NotConnectedError,
    async_discover,
)
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

_LOGGER = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 15.0
_HOST_RE = re.compile(r"[A-Za-z0-9.\-:\[\]]+")


class HubClient(Protocol):
    """The slice of LibreSyncClient this service uses; tests supply a fake."""

    host: str

    @property
    def state(self) -> DeviceState: ...
    def subscribe(self, callback: Callable[[DeviceState], None]) -> Callable[[], None]: ...
    async def async_connect(self, *, timeout: float | None = None) -> None: ...
    async def async_disconnect(self) -> None: ...
    async def async_set_power(self, on: bool) -> None: ...
    async def async_set_volume(self, level: int) -> None: ...
    async def async_select_source(self, index: int) -> None: ...
    async def async_media_play(self) -> None: ...
    async def async_media_pause(self) -> None: ...
    async def async_media_stop(self) -> None: ...
    async def async_media_next_track(self) -> None: ...
    async def async_media_previous_track(self) -> None: ...


# --- request bodies -------------------------------------------------------


class PowerBody(BaseModel):
    on: bool


class VolumeBody(BaseModel):
    level: int = Field(ge=0, le=100)


class SourceBody(BaseModel):
    index: int | None = None
    name: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "SourceBody":
        if (self.index is None) == (self.name is None):
            raise ValueError("give exactly one of 'index' or 'name'")
        return self


# --- helpers --------------------------------------------------------------


def state_to_dict(state: DeviceState) -> dict[str, Any]:
    playback = state.playback
    now = state.now_playing
    return {
        "available": state.available,
        "power": state.power,
        "volume": state.volume,
        "muted": state.muted,
        "source": (
            {"index": state.source.index, "name": state.source.name} if state.source else None
        ),
        "sources": [{"index": s.index, "name": s.name} for s in state.sources],
        "playback": playback.name.lower() if playback is not None else None,
        "now_playing": (
            {
                "title": now.title,
                "artist": now.artist,
                "album": now.album,
                "artwork_url": now.artwork_url,
                "app": now.app,
            }
            if now
            else None
        ),
        "model": state.model,
    }


def snapshot(hub: "HubClient", state: DeviceState | None = None) -> dict[str, Any]:
    """The state as JSON, plus the address the service is talking to."""
    return state_to_dict(state if state is not None else hub.state) | {"host": hub.host}


def _find_source_index(state: DeviceState, name_or_index: str) -> int:
    """Match a source by case-insensitive name, falling back to a numeric index."""
    wanted = name_or_index.strip().casefold()
    for source in state.sources:
        if source.name.casefold() == wanted:
            return source.index
    if wanted.isdigit() and any(s.index == int(wanted) for s in state.sources):
        return int(wanted)
    known = ", ".join(s.name for s in state.sources) or "none reported yet"
    raise HTTPException(404, f"no source {name_or_index!r}; known: {known}")


async def _build_client() -> LibreSyncClient:
    host = os.environ.get("STEREO_HUB_HOST", "").strip()
    if not host:
        _LOGGER.info("STEREO_HUB_HOST not set; searching the network for a hub")
        found = await async_discover()
        if not found:
            raise RuntimeError(
                "no hub found by discovery; set STEREO_HUB_HOST to the hub's IP address"
            )
        host = found[0].host
        _LOGGER.info("discovered hub %s at %s", found[0].name, host)
    elif not _HOST_RE.fullmatch(host):
        raise RuntimeError(f"STEREO_HUB_HOST={host!r} is not a hostname or IP address")
    return LibreSyncClient(host)


def _log_availability(hub: "HubClient") -> Callable[[DeviceState], None]:
    """Log each change of connection state, which is otherwise invisible."""
    last: list[bool | None] = [None]

    def on_state(state: DeviceState) -> None:
        if state.available == last[0]:
            return
        last[0] = state.available
        if state.available:
            _LOGGER.info("connected to hub at %s", hub.host)
        else:
            _LOGGER.warning(
                "hub at %s not reachable on TCP 50006 and 7777; retrying in the background",
                hub.host,
            )

    return on_state


# --- app ------------------------------------------------------------------


def create_app(client: HubClient | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        hub: HubClient = client if client is not None else await _build_client()
        _LOGGER.info("connecting to hub at %s", hub.host)
        unsubscribe = hub.subscribe(_log_availability(hub))
        # No timeout: the service comes up even while the hub is unreachable,
        # and the client reconnects by itself. `available` reports which.
        await hub.async_connect()
        app.state.hub = hub
        try:
            yield
        finally:
            unsubscribe()
            await hub.async_disconnect()

    app = FastAPI(title="Stereo Hub", version="0.1.0", lifespan=lifespan)

    def hub_of(request: Request) -> HubClient:
        return request.app.state.hub

    @app.exception_handler(NotConnectedError)
    async def _not_connected(_: Request, err: NotConnectedError) -> JSONResponse:
        return JSONResponse({"detail": f"hub not connected: {err}"}, status_code=503)

    @app.exception_handler(ConfirmationTimeout)
    async def _timeout(_: Request, err: ConfirmationTimeout) -> JSONResponse:
        return JSONResponse({"detail": f"hub did not confirm: {err}"}, status_code=504)

    @app.exception_handler(LibreSyncError)
    async def _libresync(_: Request, err: LibreSyncError) -> JSONResponse:
        return JSONResponse({"detail": str(err)}, status_code=502)

    # --- page and health ----------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> str:
        return (files("stereo_hub") / "static" / "index.html").read_text(encoding="utf-8")

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        hub = hub_of(request)
        return {"status": "ok", "hub_host": hub.host, "hub_available": hub.state.available}

    # --- REST API -----------------------------------------------------

    @app.get("/api/state")
    async def get_state(request: Request) -> dict[str, Any]:
        return snapshot(hub_of(request))

    @app.put("/api/power")
    async def put_power(request: Request, body: PowerBody) -> dict[str, Any]:
        hub = hub_of(request)
        await hub.async_set_power(body.on)
        return snapshot(hub)

    @app.put("/api/volume")
    async def put_volume(request: Request, body: VolumeBody) -> dict[str, Any]:
        hub = hub_of(request)
        await hub.async_set_volume(body.level)
        return snapshot(hub)

    @app.put("/api/source")
    async def put_source(request: Request, body: SourceBody) -> dict[str, Any]:
        hub = hub_of(request)
        index = body.index if body.index is not None else _find_source_index(hub.state, body.name or "")
        await hub.async_select_source(index)
        return snapshot(hub)

    @app.post("/api/media/{action}")
    async def post_media(
        request: Request, action: Literal["play", "pause", "stop", "next", "previous"]
    ) -> dict[str, Any]:
        hub = hub_of(request)
        commands = {
            "play": hub.async_media_play,
            "pause": hub.async_media_pause,
            "stop": hub.async_media_stop,
            "next": hub.async_media_next_track,
            "previous": hub.async_media_previous_track,
        }
        await commands[action]()
        return snapshot(hub)

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        """Server-sent events: the full state on connect and on every change."""
        hub = hub_of(request)
        # Size 1, latest wins: a slow reader skips intermediate states rather
        # than falling behind.
        queue: asyncio.Queue[DeviceState] = asyncio.Queue(maxsize=1)

        def push(state: DeviceState) -> None:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(state)

        async def stream() -> AsyncIterator[str]:
            unsubscribe = hub.subscribe(push)
            try:
                yield f"data: {json.dumps(snapshot(hub))}\n\n"
                while not await request.is_disconnected():
                    try:
                        state = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(snapshot(hub, state))}\n\n"
            finally:
                unsubscribe()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- one-shot action, for buttons ----------------------------------
    #
    # A Flic button sends a fixed request with no body, and some setups can
    # only send GET, so this is a bare URL that accepts either.

    @app.api_route("/api/actions/on", methods=["GET", "POST"])
    @app.api_route("/api/actions/on/{source}", methods=["GET", "POST"])
    async def action_on(request: Request, source: str | None = None) -> dict[str, Any]:
        """Turn the hub on and, if given, switch to `source` (name or index)."""
        hub = hub_of(request)
        await hub.async_set_power(True)
        if source is not None:
            index = _find_source_index(hub.state, source)
            if hub.state.source is None or hub.state.source.index != index:
                await hub.async_select_source(index)
        return snapshot(hub)

    return app


app = create_app()
