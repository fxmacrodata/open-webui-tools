"""
title: FXMacroData Macro & FX Data
description: Official macro releases (CPI, policy rates, payrolls, GDP), release calendars and FX rates for 22 currencies. Recent USD data works without an API key.
author: FXMacroData
author_url: https://fxmacrodata.com
requirements:aiohttp
version: 0.1.0
license: MIT
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import aiohttp
from pydantic import BaseModel, Field

BASE_URL = "https://api.fxmacrodata.com/v1"
CURRENCY_PATTERN = re.compile(r"^[A-Za-z]{3}$")
SLUG_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_LIMIT = 100
STATUS_DETAILS = {
    400: "the request was invalid",
    401: "an API key is required for this request, or the key was rejected",
    403: "the API key does not cover this request",
    404: "the currency or indicator was not found",
    422: "the request parameters were rejected",
    429: "the rate limit was exceeded",
}


class FXMacroDataError(Exception):
    """A model-readable error that never contains the API key."""


async def emit_status(
    event_emitter: Callable[[Any], Awaitable[None]] | None,
    description: str,
    done: bool = False,
) -> None:
    """Emit an Open WebUI status event when an emitter is available."""
    if event_emitter:
        await event_emitter(
            {"type": "status", "data": {"description": description, "done": done}}
        )


def validate_currency(value: str, label: str) -> str:
    """Return a lowercase three-letter currency code."""
    text = value.strip() if isinstance(value, str) else ""
    if not CURRENCY_PATTERN.fullmatch(text):
        raise FXMacroDataError(
            f"{label} must be a three-letter currency code such as USD"
        )
    return text.lower()


def validate_indicator(value: str, required: bool = True) -> str:
    """Return a trimmed lowercase indicator slug such as inflation."""
    text = value.strip().lower() if isinstance(value, str) else ""
    if not text and not required:
        return ""
    if not SLUG_PATTERN.fullmatch(text):
        raise FXMacroDataError(
            "indicator must be a slug such as inflation or policy_rate; "
            "use list_indicators to see the slugs for a currency"
        )
    return text


def validate_date(value: str, label: str) -> date | None:
    """Parse an optional YYYY-MM-DD date that must exist on the calendar."""
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return None
    if not DATE_PATTERN.fullmatch(text):
        raise FXMacroDataError(f"{label} must be a date in YYYY-MM-DD form")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise FXMacroDataError(f"{label} is not a real calendar date") from None


def date_window(start_date: str, end_date: str) -> dict[str, str]:
    """Validate an optional date range and return it as query parameters."""
    start = validate_date(start_date, "start_date")
    end = validate_date(end_date, "end_date")
    if start and end and start > end:
        raise FXMacroDataError("start_date must be on or before end_date")
    window = {}
    if start:
        window["start_date"] = start.isoformat()
    if end:
        window["end_date"] = end.isoformat()
    return window


def validate_limit(value: int) -> int:
    """Bound row counts to the API maximum, rejecting booleans."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise FXMacroDataError("limit must be an integer")
    if value < 1 or value > MAX_LIMIT:
        raise FXMacroDataError(f"limit must be between 1 and {MAX_LIMIT}")
    return value


def data_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the data list, refusing any other shape."""
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise FXMacroDataError("FXMacroData returned no data list")
    return [row for row in rows if isinstance(row, dict)]


def has_more(payload: dict[str, Any]) -> bool | None:
    """Return pagination.has_more, refusing a present but malformed object."""
    if "pagination" not in payload:
        return None
    page = payload["pagination"]
    if not isinstance(page, dict) or not isinstance(page.get("has_more"), bool):
        raise FXMacroDataError("FXMacroData returned malformed pagination")
    return page["has_more"]


def keyless_notices(payload: dict[str, Any]) -> list[str]:
    """Keyless responses say when history is cut to 90 days or delayed."""
    notices = []
    for field in ("freemium_window", "freemium_delay"):
        block = payload.get(field)
        if isinstance(block, dict) and block.get("applied") is True:
            message = block.get("message")
            if isinstance(message, str) and message.strip():
                notices.append(message.strip())
    return notices


class Tools:
    """Read-only Open WebUI tools for FXMacroData macro and FX data."""

    class Valves(BaseModel):
        """Administrator-controlled request and response bounds."""

        REQUEST_TIMEOUT_SECONDS: int = Field(
            default=30,
            ge=5,
            le=120,
            description="FXMacroData request timeout in seconds",
        )

    class UserValves(BaseModel):
        """Per-user settings. Each user brings their own key."""

        FXMACRODATA_API_KEY: str = Field(
            default="",
            description=(
                "Optional FXMacroData API key. Without one, recent USD releases, the "
                "USD calendar and every currency's indicator list still work."
            ),
            json_schema_extra={"input": {"type": "password"}},
        )

    def __init__(self) -> None:
        """Initialize default Open WebUI valve values."""
        self.valves = self.Valves()
        self.citation = False

    @staticmethod
    def _user_key(user: dict[str, Any] | None) -> str:
        """Read the calling user's key from Open WebUI user valves."""
        valves = (user or {}).get("valves") if isinstance(user, dict) else None
        if isinstance(valves, dict):
            key = valves.get("FXMACRODATA_API_KEY", "")
        else:
            key = getattr(valves, "FXMACRODATA_API_KEY", "")
        return key.strip() if isinstance(key, str) else ""

    async def _get_json(
        self, path: str, params: dict[str, str], api_key: str
    ) -> dict[str, Any]:
        """GET one fixed-origin path. Redirects are refused so the key never moves."""
        headers = {"accept": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        timeout = aiohttp.ClientTimeout(total=self.valves.REQUEST_TIMEOUT_SECONDS)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(
                    f"{BASE_URL}{path}",
                    headers=headers,
                    params=params,
                    allow_redirects=False,
                ) as response,
            ):
                if 300 <= response.status < 400:
                    raise FXMacroDataError(
                        "FXMacroData answered with an unexpected redirect"
                    )
                if response.status != 200:
                    detail = STATUS_DETAILS.get(response.status, "the request failed")
                    raise FXMacroDataError(
                        f"FXMacroData returned HTTP {response.status}: {detail}"
                    )
                try:
                    payload = await response.json(content_type=None)
                except (json.JSONDecodeError, ValueError, aiohttp.ContentTypeError):
                    raise FXMacroDataError(
                        "FXMacroData returned a non-JSON response"
                    ) from None
        except asyncio.TimeoutError:
            raise FXMacroDataError("The FXMacroData request timed out") from None
        except (aiohttp.ClientError, ValueError):
            # Transport errors can quote the request; never interpolate them.
            raise FXMacroDataError("Could not reach FXMacroData") from None
        if not isinstance(payload, dict):
            raise FXMacroDataError("FXMacroData returned an unexpected response shape")
        if "detail" in payload and "data" not in payload:
            raise FXMacroDataError("FXMacroData returned an error response")
        return payload

    @staticmethod
    def _format(title: str, normalized: Any, api_key: str) -> str:
        text = (
            f"{title}\n\n"
            f"```json\n{json.dumps(normalized, indent=2, ensure_ascii=False)}\n```\n\n"
            "Source: FXMacroData (https://fxmacrodata.com/documentation)"
        )
        return text.replace(api_key, "[REDACTED]") if api_key else text

    async def _run(self, title, work, user, event_emitter) -> str:
        api_key = self._user_key(user)
        await emit_status(event_emitter, f"FXMacroData: {title}")
        try:
            return self._format(title, await work(api_key), api_key)
        except FXMacroDataError as exc:
            message = str(exc)
            if api_key:
                message = message.replace(api_key, "[REDACTED]")
            return f"{title} failed: {message}"
        finally:
            await emit_status(event_emitter, "FXMacroData lookup finished", done=True)

    async def get_indicator_history(
        self,
        currency: str,
        indicator: str,
        start_date: str = "",
        end_date: str = "",
        limit: int = 12,
        __user__: dict[str, Any] | None = None,
        __event_emitter__: Callable[[Any], Awaitable[None]] | None = None,
    ) -> str:
        """
        Get the release history of one macro indicator: value, previous value,
        reference period and publication time of each release.

        Args:
            currency: Three-letter currency code, such as USD or EUR.
            indicator: Indicator slug, such as inflation, policy_rate,
                non_farm_payrolls, gdp or unemployment.
            start_date: Optional start date, YYYY-MM-DD.
            end_date: Optional end date, YYYY-MM-DD.
            limit: Releases to return, from 1 to 100.
        """

        async def work(api_key):
            ccy = validate_currency(currency, "currency")
            slug = validate_indicator(indicator)
            params = {
                **date_window(start_date, end_date),
                "limit": str(validate_limit(limit)),
            }
            payload = await self._get_json(
                f"/announcements/{ccy}/{slug}", params, api_key
            )
            return {
                "currency": ccy.upper(),
                "indicator": slug,
                "name": payload.get("name"),
                "releases": [
                    {
                        "period": row.get("date"),
                        "value": row.get("val"),
                        "previous_value": row.get("previous_value"),
                        "published_local": row.get("announcement_datetime_local"),
                        "source_url": row.get("source_url"),
                    }
                    for row in data_rows(payload)
                ],
                "has_more": has_more(payload),
                "notices": keyless_notices(payload),
            }

        return await self._run("Indicator history", work, __user__, __event_emitter__)

    async def get_release_calendar(
        self,
        currency: str,
        indicator: str = "",
        limit: int = 20,
        __user__: dict[str, Any] | None = None,
        __event_emitter__: Callable[[Any], Awaitable[None]] | None = None,
    ) -> str:
        """
        List upcoming scheduled data releases for a currency.

        Args:
            currency: Three-letter currency code, such as USD.
            indicator: Optional indicator slug to show only that release.
            limit: Releases to return, from 1 to 100.
        """

        async def work(api_key):
            ccy = validate_currency(currency, "currency")
            slug = validate_indicator(indicator, required=False)
            count = validate_limit(limit)
            params = {"indicator": slug} if slug else {}
            payload = await self._get_json(f"/calendar/{ccy}", params, api_key)
            return {
                "currency": ccy.upper(),
                "events": [
                    {
                        "release": row.get("release"),
                        "name": row.get("name"),
                        "release_time_utc": row.get("announcement_datetime_utc"),
                        "reference_period": row.get("reference_period")
                        or row.get("date"),
                        "importance": row.get("event_importance"),
                    }
                    for row in data_rows(payload)[:count]
                ],
            }

        return await self._run("Release calendar", work, __user__, __event_emitter__)

    async def list_indicators(
        self,
        currency: str,
        __user__: dict[str, Any] | None = None,
        __event_emitter__: Callable[[Any], Awaitable[None]] | None = None,
    ) -> str:
        """
        List the indicator slugs published for a currency, with units and the
        latest available date. Use this before get_indicator_history.

        Args:
            currency: Three-letter currency code, such as JPY.
        """

        async def work(api_key):
            ccy = validate_currency(currency, "currency")
            payload = await self._get_json(f"/data_catalogue/{ccy}", {}, api_key)
            indicators = []
            for slug, meta in payload.items():
                if not isinstance(meta, dict):
                    continue
                coverage = meta.get("coverage")
                coverage = coverage if isinstance(coverage, dict) else {}
                indicators.append(
                    {
                        "indicator": slug,
                        "name": meta.get("name"),
                        "unit": meta.get("unit"),
                        "latest_available_date": coverage.get("latest_available_date"),
                        "requires_api_key": coverage.get("requires_api_key"),
                    }
                )
            if not indicators:
                raise FXMacroDataError("FXMacroData returned an empty catalogue")
            return {"currency": ccy.upper(), "indicators": indicators}

        return await self._run("Indicator list", work, __user__, __event_emitter__)

    async def get_fx_rates(
        self,
        base: str,
        quote: str,
        start_date: str = "",
        end_date: str = "",
        limit: int = 30,
        __user__: dict[str, Any] | None = None,
        __event_emitter__: Callable[[Any], Awaitable[None]] | None = None,
    ) -> str:
        """
        Get daily FX rates for a currency pair. Needs an FXMacroData API key.

        Args:
            base: Base currency, such as EUR.
            quote: Quote currency, such as USD.
            start_date: Optional start date, YYYY-MM-DD.
            end_date: Optional end date, YYYY-MM-DD.
            limit: Days to return, from 1 to 100.
        """

        async def work(api_key):
            base_ccy = validate_currency(base, "base")
            quote_ccy = validate_currency(quote, "quote")
            params = {
                **date_window(start_date, end_date),
                "limit": str(validate_limit(limit)),
            }
            payload = await self._get_json(
                f"/forex/{base_ccy}/{quote_ccy}", params, api_key
            )
            return {
                "pair": f"{base_ccy.upper()}/{quote_ccy.upper()}",
                "rates": [
                    {"date": row.get("date"), "rate": row.get("val")}
                    for row in data_rows(payload)
                ],
                "has_more": has_more(payload),
            }

        return await self._run("FX rates", work, __user__, __event_emitter__)
