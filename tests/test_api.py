"""Tests du client HTTP (api.py)."""
from __future__ import annotations

import pytest
from aiohttp import ClientSession
from aioresponses import aioresponses

from custom_components.plugchoice.api import (
    PlugchoiceApiError,
    PlugchoiceAuthError,
    PlugchoiceClient,
    _parse_log_params,
)
from custom_components.plugchoice.const import API_BASE_URL


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Appel OCPP brut [MessageTypeId, UniqueId, Action, Payload] -> payload
        (
            '[2, "uid-1", "SetChargingProfile", {"connectorId": 1, "csChargingProfiles": {}}]',
            {"connectorId": 1, "csChargingProfiles": {}},
        ),
        ('{"connectorId": 2}', {"connectorId": 2}),
        ("pas du json", None),
        (None, None),
        ("[]", None),
        ([1, 2, {"a": 1}, {"b": 2}], {"b": 2}),
    ],
)
def test_parse_log_params(raw, expected):
    assert _parse_log_params(raw) == expected


async def test_get_user_ok():
    with aioresponses() as mock:
        mock.get(f"{API_BASE_URL}/user", payload={"uuid": "u1"})
        async with ClientSession() as session:
            client = PlugchoiceClient(session, "token")
            assert await client.async_get_user() == {"uuid": "u1"}


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_error(status):
    with aioresponses() as mock:
        mock.get(f"{API_BASE_URL}/user", status=status)
        async with ClientSession() as session:
            client = PlugchoiceClient(session, "token")
            with pytest.raises(PlugchoiceAuthError):
                await client.async_get_user()


async def test_server_error_becomes_api_error():
    with aioresponses() as mock:
        mock.get(f"{API_BASE_URL}/user", status=500, body="boom")
        async with ClientSession() as session:
            client = PlugchoiceClient(session, "token")
            with pytest.raises(PlugchoiceApiError):
                await client.async_get_user()


async def test_timeout_becomes_api_error():
    with aioresponses() as mock:
        mock.get(f"{API_BASE_URL}/user", exception=TimeoutError())
        async with ClientSession() as session:
            client = PlugchoiceClient(session, "token")
            with pytest.raises(PlugchoiceApiError):
                await client.async_get_user()


async def test_list_chargers_follows_pagination():
    with aioresponses() as mock:
        mock.get(
            f"{API_BASE_URL}/chargers",
            payload={"data": [{"uuid": "a"}], "links": {"next": f"{API_BASE_URL}/chargers?page=2"}},
        )
        mock.get(
            f"{API_BASE_URL}/chargers?page=2",
            payload={"data": [{"uuid": "b"}], "links": {"next": None}},
        )
        async with ClientSession() as session:
            client = PlugchoiceClient(session, "token")
            chargers = await client.async_list_chargers()
    assert [c["uuid"] for c in chargers] == ["a", "b"]
