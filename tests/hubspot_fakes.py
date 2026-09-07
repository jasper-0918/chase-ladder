"""A fake HTTP transport, shared by the HubSpot unit tests and the integration test.

It stands in for httpx.Client: same `request(method, url, **kwargs)` shape, no network.
Responses are queued per (method, path). A route with one response returns it for every
call, which is what PATCH routes want; a route with several pops them in order, which is
how the 429-then-success cases are written.
"""

from __future__ import annotations

PIPELINES = {
    "results": [
        {
            "id": "default",
            "label": "Sales Pipeline",
            "stages": [
                {"id": "s-draft", "label": "Draft", "displayOrder": 0},
                {"id": "s-sent", "label": "Sent", "displayOrder": 1},
                {"id": "s-chasing", "label": "Chasing", "displayOrder": 2},
                {"id": "s-replied", "label": "Replied", "displayOrder": 3},
                {"id": "s-won", "label": "Won", "displayOrder": 4},
                {"id": "s-lost", "label": "Lost", "displayOrder": 5},
            ],
        }
    ]
}


class Response:
    def __init__(self, status_code=200, json_body=None, headers=None):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.headers = headers or {}
        self.text = str(self._json)

    def json(self):
        return self._json


class FakeHTTP:
    """Queues one or more responses per (method, path) and records every call."""

    def __init__(self, routes=None):
        self.routes = {k: list(v) for k, v in (routes or {}).items()}
        self.calls = []

    def request(self, method, url, **kwargs):
        path = url.split("api.hubapi.com", 1)[-1].split("?", 1)[0]
        self.calls.append(
            {
                "method": method,
                "url": url,
                "path": path,
                "json": kwargs.get("json"),
                "params": kwargs.get("params"),
                "headers": kwargs.get("headers"),
            }
        )
        queued = self.routes.get((method, path))
        if not queued:
            raise AssertionError(f"no response queued for {method} {path}")
        return queued.pop(0) if len(queued) > 1 else queued[0]

    def bodies(self, method, path):
        """Every request body sent to one route, in order."""
        return [c["json"] for c in self.calls if c["method"] == method and c["path"] == path]


class FakeSleep:
    def __init__(self):
        self.slept = []

    def __call__(self, seconds):
        self.slept.append(seconds)


def search_body(results):
    return {"total": len(results), "results": results}


def deal(
    deal_id="1",
    stage="s-sent",
    name="Website rebuild",
    amount="12000",
    quote_sent_at="2026-08-20",
    last_chase_at=None,
):
    return {
        "id": deal_id,
        "properties": {
            "dealname": name,
            "amount": amount,
            "dealstage": stage,
            "quote_sent_at": quote_sent_at,
            "last_chase_at": last_chase_at,
        },
    }
