from collections.abc import Callable

import pytest
from aiolibresync import DeviceState, NotConnectedError, Source
from fastapi.testclient import TestClient

from stereo_hub.app import create_app

SOURCES = (Source(0, "Streaming"), Source(1, "HDMI"), Source(2, "Optical"))


class FakeHub:
    """Applies commands to its state immediately, as a confirmed hub would."""

    host = "192.0.2.10"

    def __init__(self, **state) -> None:
        defaults = dict(
            available=True, power=False, volume=20, sources=SOURCES, source=SOURCES[0]
        )
        self._state = DeviceState(**(defaults | state))
        self.calls: list[tuple[str, object]] = []
        self.subscribers: list[Callable[[DeviceState], None]] = []
        self.connected = False

    @property
    def state(self) -> DeviceState:
        return self._state

    def _set(self, **changes) -> None:
        self._state = self._state.evolve(**changes)
        for cb in list(self.subscribers):
            cb(self._state)

    def subscribe(self, callback):
        self.subscribers.append(callback)
        return lambda: self.subscribers.remove(callback)

    async def async_connect(self, *, timeout=None) -> None:
        self.connected = True

    async def async_disconnect(self) -> None:
        self.connected = False

    def _require(self) -> None:
        if not self._state.available:
            raise NotConnectedError("down")

    async def async_set_power(self, on: bool) -> None:
        self._require()
        self.calls.append(("power", on))
        self._set(power=on)

    async def async_set_volume(self, level: int) -> None:
        self._require()
        self.calls.append(("volume", level))
        self._set(volume=level)

    async def async_select_source(self, index: int) -> None:
        self._require()
        self.calls.append(("source", index))
        self._set(source=next(s for s in SOURCES if s.index == index))

    async def _media(self, name: str) -> None:
        self._require()
        self.calls.append(("media", name))

    async def async_media_play(self) -> None:
        await self._media("play")

    async def async_media_pause(self) -> None:
        await self._media("pause")

    async def async_media_stop(self) -> None:
        await self._media("stop")

    async def async_media_next_track(self) -> None:
        await self._media("next")

    async def async_media_previous_track(self) -> None:
        await self._media("previous")


@pytest.fixture
def hub() -> FakeHub:
    return FakeHub()


@pytest.fixture
def client(hub: FakeHub):
    with TestClient(create_app(hub)) as c:
        yield c


def test_lifespan_connects_and_disconnects(hub: FakeHub) -> None:
    with TestClient(create_app(hub)):
        assert hub.connected
    assert not hub.connected


def test_index_page(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "<title>Stereo Hub</title>" in res.text


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {
        "status": "ok",
        "hub_host": "192.0.2.10",
        "hub_available": True,
    }


def test_state(client: TestClient) -> None:
    body = client.get("/api/state").json()
    assert body["power"] is False
    assert body["volume"] == 20
    assert body["source"] == {"index": 0, "name": "Streaming"}
    assert [s["name"] for s in body["sources"]] == ["Streaming", "HDMI", "Optical"]
    assert body["host"] == "192.0.2.10"


def test_power(client: TestClient, hub: FakeHub) -> None:
    assert client.put("/api/power", json={"on": True}).json()["power"] is True
    assert hub.calls == [("power", True)]


def test_volume(client: TestClient, hub: FakeHub) -> None:
    assert client.put("/api/volume", json={"level": 42}).json()["volume"] == 42
    assert client.put("/api/volume", json={"level": 101}).status_code == 422


def test_source_by_index_and_name(client: TestClient, hub: FakeHub) -> None:
    assert client.put("/api/source", json={"index": 2}).json()["source"]["name"] == "Optical"
    assert client.put("/api/source", json={"name": "hdmi"}).json()["source"]["name"] == "HDMI"
    assert client.put("/api/source", json={"name": "Phono"}).status_code == 404
    assert client.put("/api/source", json={}).status_code == 422


def test_media(client: TestClient, hub: FakeHub) -> None:
    assert client.post("/api/media/pause").status_code == 200
    assert client.post("/api/media/rewind").status_code == 422
    assert hub.calls == [("media", "pause")]


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_button_action_powers_on_and_selects(client: TestClient, hub: FakeHub, method) -> None:
    body = client.request(method, "/api/actions/on/HDMI").json()
    assert body["power"] is True
    assert body["source"]["name"] == "HDMI"
    assert hub.calls == [("power", True), ("source", 1)]


def test_button_action_skips_reselecting_current_source(client: TestClient, hub: FakeHub) -> None:
    client.get("/api/actions/on/streaming")
    assert hub.calls == [("power", True)]


def test_button_action_power_only(client: TestClient, hub: FakeHub) -> None:
    assert client.get("/api/actions/on").json()["power"] is True
    assert hub.calls == [("power", True)]


def test_button_action_unknown_source(client: TestClient) -> None:
    assert client.get("/api/actions/on/Phono").status_code == 404


def test_hub_offline_is_503() -> None:
    hub = FakeHub(available=False)
    with TestClient(create_app(hub)) as c:
        res = c.put("/api/power", json={"on": True})
    assert res.status_code == 503


def test_connection_changes_are_logged(caplog) -> None:
    hub = FakeHub(available=False)
    with caplog.at_level("INFO", logger="stereo_hub.app"), TestClient(create_app(hub)):
        hub._set(available=True)
        hub._set(volume=30)  # no change in availability: not logged again
        hub._set(available=False)
    messages = [r.getMessage() for r in caplog.records]
    assert "connecting to hub at 192.0.2.10" in messages
    assert "connected to hub at 192.0.2.10" in messages
    assert sum("not reachable" in m for m in messages) == 1


@pytest.mark.parametrize("host", ["<hub-ip>", "192.168.x.x y", "a/b"])
def test_placeholder_host_is_rejected(monkeypatch, host) -> None:
    monkeypatch.setenv("STEREO_HUB_HOST", host)
    with pytest.raises(RuntimeError, match="not a hostname or IP"):
        with TestClient(create_app()):
            pass


def test_get_volume_and_source(client: TestClient) -> None:
    assert client.get("/api/volume").json() == {"level": 20}
    assert client.get("/api/source").json() == {"index": 0, "name": "Streaming"}


def test_get_volume_and_source_before_hub_reports() -> None:
    with TestClient(create_app(FakeHub(volume=None, source=None))) as c:
        assert c.get("/api/volume").json() == {"level": None}
        assert c.get("/api/source").json() == {"index": None, "name": None}
