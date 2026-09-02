"""Tests du client HTTP (api.py)."""
from __future__ import annotations

import pytest
from aiohttp import ClientSession
from aioresponses import aioresponses
from yarl import URL

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


async def test_set_charging_limit_body_matches_working_profile():
    """Corps = profil confirmé fonctionnel : stackLevel 4 + numberPhases 3."""
    url = f"{API_BASE_URL}/chargers/c1/actions/charge-limit"
    with aioresponses() as mock:
        mock.post(url, payload={"status": "Accepted"})
        async with ClientSession() as session:
            client = PlugchoiceClient(session, "token")
            await client.async_set_charging_limit("c1", 1, 16)
        call = mock.requests[("POST", URL(url))][0]
    assert call.kwargs["json"] == {
        "connector_id": 1,
        "limit": 16,
        "stack_level": 4,
        "number_phases": 3,
    }


async def test_clear_charging_limit_targets_our_stack_level():
    url = f"{API_BASE_URL}/chargers/c1/actions/clear-charge-limit"
    with aioresponses() as mock:
        mock.post(url, payload={"status": "Accepted"})
        async with ClientSession() as session:
            client = PlugchoiceClient(session, "token")
            result = await client.async_clear_charging_limit("c1", 1)
        call = mock.requests[("POST", URL(url))][0]
    assert result == {"status": "Accepted"}
    # ne cible que le stackLevel de nos propres commandes, pas tous les profils
    assert call.kwargs["json"] == {"connector_id": 1, "stack_level": 4}


async def test_clear_charging_limit_all_profiles_when_stack_none():
    url = f"{API_BASE_URL}/chargers/c1/actions/clear-charge-limit"
    with aioresponses() as mock:
        mock.post(url, payload={})
        async with ClientSession() as session:
            client = PlugchoiceClient(session, "token")
            await client.async_clear_charging_limit("c1", 1, stack_level=None)
        call = mock.requests[("POST", URL(url))][0]
    assert call.kwargs["json"] == {"connector_id": 1}


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
