"""Shared fixtures. Tests never touch the network: HTTP is served by respx fakes."""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import respx

type Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("queensestate.arcgis._backoff_seconds", lambda _attempt: 0.0)


def arcgis_response(
    request: httpx.Request,
    features: list[dict[str, Any]] | None = None,
    *,
    statistics: list[dict[str, Any]] | None = None,
    date_fields: tuple[str, ...] = (),
) -> httpx.Response:
    """Answer an ArcGIS ``query`` request the way the real service shapes its JSON."""
    features = features or []
    params = request.url.params
    if params.get("returnCountOnly") == "true":
        return httpx.Response(200, json={"count": len(features)})
    fields = [{"name": name, "type": "esriFieldTypeDate"} for name in date_fields]
    if "outStatistics" in params:
        rows = [{"attributes": row} for row in statistics or []]
        return httpx.Response(200, json={"fields": fields, "features": rows})
    return httpx.Response(200, json={"fields": fields, "features": features})


def feature(
    attributes: dict[str, Any], longitude: float | None = None, latitude: float | None = None
) -> dict[str, Any]:
    """An Esri JSON feature, with point geometry when coordinates are given."""
    if longitude is None or latitude is None:
        return {"attributes": attributes}
    return {"attributes": attributes, "geometry": {"x": longitude, "y": latitude}}


def statistics_request(request: httpx.Request) -> list[dict[str, str]]:
    decoded: list[dict[str, str]] = json.loads(request.url.params["outStatistics"])
    return decoded


@dataclass
class FakeArcGIS:
    """Routes every GET to a handler registered for its layer URL (404 when unregistered)."""

    router: respx.MockRouter
    handlers: dict[str, Handler] = field(default_factory=dict)

    def serve(self, layer_url: str, handler: Handler) -> None:
        self.handlers[layer_url] = handler

    def requests_to(self, layer_url: str) -> list[httpx.Request]:
        return [call.request for call in self.router.calls if _layer_of(call.request) == layer_url]

    def dispatch(self, request: httpx.Request) -> httpx.Response:
        handler = self.handlers.get(_layer_of(request))
        return handler(request) if handler else httpx.Response(404)


def _layer_of(request: httpx.Request) -> str:
    return str(request.url.copy_with(query=None)).removesuffix("/query")


@pytest.fixture
def fake_arcgis() -> Iterator[FakeArcGIS]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        fake = FakeArcGIS(router)
        router.route(method="GET").mock(side_effect=fake.dispatch)
        yield fake
