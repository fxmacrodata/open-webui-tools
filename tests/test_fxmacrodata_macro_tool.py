"""Focused tests for the FXMacroData Open WebUI tool."""

import asyncio
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest

MODULE_PATH = Path(__file__).parents[1] / "tools" / "fxmacrodata_macro_tool.py"
SPEC = importlib.util.spec_from_file_location("fxmacrodata_macro_tool", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

KEY = "fxmd-secret-key-123456"
USER = {"valves": {"FXMACRODATA_API_KEY": f" {KEY} "}}


class FakeResponse:
    """Async response context manager used by the fake aiohttp session."""

    def __init__(self, status=200, payload=None, raw=None):
        self.status = status
        self.payload = payload if payload is not None else {}
        self.raw = raw

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self, content_type=None):
        if self.raw is not None:
            return json.loads(self.raw)
        return self.payload


class FakeSession:
    """Capture request arguments and return a configured response or error."""

    def __init__(self, response, calls, timeout=None):
        self.response = response
        self.calls = calls
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs, "timeout": self.timeout})
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def run(coro_factory, response, calls=None):
    calls = [] if calls is None else calls

    def factory(timeout=None):
        return FakeSession(response, calls, timeout=timeout)

    with patch.object(MODULE.aiohttp, "ClientSession", factory):
        return asyncio.run(coro_factory())


def parsed_json(result):
    """Extract the fenced JSON object from a formatted tool response."""
    return json.loads(result.split("```json\n", 1)[1].split("\n```", 1)[0])


def history_payload(**extra):
    payload = {
        "name": "Inflation (CPI)",
        "pagination": {"has_more": False, "total_count": 1},
        "freemium_window": {
            "applied": True,
            "message": "Anonymous access returns the most recent 90 days.",
        },
        "freemium_delay": {
            "applied": True,
            "message": "Free access is delayed by 15 minutes.",
        },
        "data": [
            {
                "date": "2026-08-31",
                "val": 3.4,
                "previous_value": 3.4,
                "announcement_datetime_local": "2026-09-11T08:30:00-04:00",
                "source_url": "https://www.bls.gov/",
            }
        ],
    }
    payload.update(extra)
    return payload


def test_keyless_history_sends_no_key_and_refuses_redirects():
    calls = []
    tool = MODULE.Tools()
    result = run(
        lambda: tool.get_indicator_history(
            " usd ",
            "Inflation",
            start_date="2026-07-01",
            end_date="2026-09-30",
            limit=5,
        ),
        FakeResponse(payload=history_payload()),
        calls,
    )
    assert (
        calls[0]["url"] == "https://api.fxmacrodata.com/v1/announcements/usd/inflation"
    )
    assert calls[0]["allow_redirects"] is False
    assert "X-API-Key" not in calls[0]["headers"]
    assert calls[0]["params"] == {
        "start_date": "2026-07-01",
        "end_date": "2026-09-30",
        "limit": "5",
    }
    assert calls[0]["timeout"].total == 30
    output = parsed_json(result)
    assert output["releases"][0]["value"] == 3.4
    assert output["releases"][0]["period"] == "2026-08-31"
    assert output["has_more"] is False
    assert len(output["notices"]) == 2


def test_user_key_is_sent_as_header_and_redacted_from_output():
    calls = []
    body = history_payload()
    body["data"][0]["source_url"] = f"https://example.test/?k={KEY}"
    tool = MODULE.Tools()
    result = run(
        lambda: tool.get_indicator_history("EUR", "policy_rate", __user__=USER),
        FakeResponse(payload=body),
        calls,
    )
    assert calls[0]["headers"]["X-API-Key"] == KEY
    assert KEY not in result
    assert "[REDACTED]" in result


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_is_an_error(status):
    tool = MODULE.Tools()
    result = run(
        lambda: tool.get_release_calendar("USD", __user__=USER),
        FakeResponse(status=status),
    )
    assert "unexpected redirect" in result
    assert KEY not in result


@pytest.mark.parametrize("status", [401, 403, 404, 422, 429, 500])
def test_http_errors_do_not_echo_the_body(status):
    tool = MODULE.Tools()
    response = FakeResponse(status=status, payload={"detail": f"bad key {KEY}"})
    result = run(lambda: tool.get_fx_rates("EUR", "USD", __user__=USER), response)
    assert f"HTTP {status}" in result
    assert KEY not in result


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            aiohttp.ClientConnectionError(f"X-API-Key: {KEY}"),
            "Could not reach FXMacroData",
        ),
        (asyncio.TimeoutError(), "timed out"),
        (ValueError(f"Invalid header value {KEY}"), "Could not reach FXMacroData"),
    ],
)
def test_transport_errors_are_never_interpolated(error, message):
    tool = MODULE.Tools()
    result = run(lambda: tool.list_indicators("USD", __user__=USER), error)
    assert message in result
    assert KEY not in result


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(raw="<html>oops</html>"),
        FakeResponse(payload=[1, 2]),
        FakeResponse(payload={"detail": "This endpoint requires an API key"}),
        FakeResponse(payload={"data": {"not": "a list"}}),
        FakeResponse(payload=history_payload(pagination=[])),
        FakeResponse(payload=history_payload(pagination={"has_more": "no"})),
    ],
)
def test_bad_200_bodies_are_errors(response):
    tool = MODULE.Tools()
    result = run(lambda: tool.get_indicator_history("USD", "inflation"), response)
    assert result.startswith("Indicator history failed:")


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda t: t.get_indicator_history("US", "inflation"), "three-letter"),
        (lambda t: t.get_indicator_history("USD", "c p i"), "slug"),
        (lambda t: t.get_indicator_history("USD", ""), "slug"),
        (
            lambda t: t.get_indicator_history("USD", "cpi", start_date="2026/01/01"),
            "YYYY-MM-DD",
        ),
        (
            lambda t: t.get_indicator_history("USD", "cpi", start_date="2026-02-30"),
            "real calendar",
        ),
        (
            lambda t: t.get_indicator_history(
                "USD", "cpi", start_date="2026-05-01", end_date="2026-04-01"
            ),
            "on or before",
        ),
        (
            lambda t: t.get_indicator_history("USD", "cpi", limit=101),
            "between 1 and 100",
        ),
        (lambda t: t.get_indicator_history("USD", "cpi", limit=True), "integer"),
        (lambda t: t.get_fx_rates("EUR", "U$D"), "quote must be"),
    ],
)
def test_invalid_input_never_calls_the_api(call, message):
    calls = []
    tool = MODULE.Tools()
    result = run(lambda: call(tool), FakeResponse(payload={"data": []}), calls)
    assert message in result
    assert calls == []


def test_calendar_filters_and_truncates():
    calls = []
    rows = [
        {
            "release": f"r{index}",
            "name": "Nonfarm Payrolls",
            "announcement_datetime_utc": "2026-11-06T13:30:00+00:00",
            "reference_period": "October 2026",
            "event_importance": "high",
        }
        for index in range(5)
    ]
    tool = MODULE.Tools()
    result = run(
        lambda: tool.get_release_calendar(
            "USD", indicator="non_farm_payrolls", limit=2
        ),
        FakeResponse(payload={"data": rows}),
        calls,
    )
    assert calls[0]["params"] == {"indicator": "non_farm_payrolls"}
    events = parsed_json(result)["events"]
    assert [event["release"] for event in events] == ["r0", "r1"]
    assert events[0]["reference_period"] == "October 2026"


def test_list_indicators_summarises_and_rejects_empty():
    body = {
        "inflation": {
            "name": "Inflation (CPI)",
            "unit": "%",
            "coverage": {
                "latest_available_date": "2026-08-31",
                "requires_api_key": False,
            },
        }
    }
    tool = MODULE.Tools()
    result = run(lambda: tool.list_indicators("usd"), FakeResponse(payload=body))
    assert parsed_json(result)["indicators"] == [
        {
            "indicator": "inflation",
            "name": "Inflation (CPI)",
            "unit": "%",
            "latest_available_date": "2026-08-31",
            "requires_api_key": False,
        }
    ]
    empty = run(lambda: tool.list_indicators("usd"), FakeResponse(payload={}))
    assert "empty catalogue" in empty


def test_fx_rates_map_rows():
    calls = []
    body = {
        "data": [{"date": "2026-10-06", "val": 1.0931}],
        "pagination": {"has_more": True},
    }
    tool = MODULE.Tools()
    result = run(
        lambda: tool.get_fx_rates("eur", "usd", limit=1, __user__=USER),
        FakeResponse(payload=body),
        calls,
    )
    assert calls[0]["url"].endswith("/forex/eur/usd")
    output = parsed_json(result)
    assert output["pair"] == "EUR/USD"
    assert output["rates"] == [{"date": "2026-10-06", "rate": 1.0931}]
    assert output["has_more"] is True


def test_user_valves_object_is_accepted():
    tool = MODULE.Tools()
    user = {"valves": MODULE.Tools.UserValves(FXMACRODATA_API_KEY=KEY)}
    assert tool._user_key(user) == KEY
    assert tool._user_key(None) == ""
    assert tool._user_key({"valves": None}) == ""
