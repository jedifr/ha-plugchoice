"""Tests du config flow (identifiant unique, anti-doublon)."""
from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.plugchoice.const import DOMAIN


def _patched_client(user: dict, chargers: list[dict] | None = None):
    client = AsyncMock()
    client.async_get_user = AsyncMock(return_value=user)
    client.async_list_chargers = AsyncMock(
        return_value=chargers if chargers is not None else [{"uuid": "c1"}]
    )
    return patch(
        "custom_components.plugchoice.config_flow.PlugchoiceClient", return_value=client
    )


async def _run_flow(hass, token: str):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": token}
    )
    # étape "badges" : on termine sans rien ajouter
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"finish": True}
    )


async def test_unique_id_uses_uuid(hass):
    with _patched_client({"uuid": "user-42", "name": "Compte"}):
        result = await _run_flow(hass, "tok")
    assert result["type"] == FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == "user-42"


async def test_unique_id_hashes_token_as_last_resort(hass):
    """Sans uuid ni email, l'identifiant est un hash — jamais le token en clair."""
    with _patched_client({"name": "Compte sans uuid"}):
        result = await _run_flow(hass, "secret-token-123")
    assert result["type"] == FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    expected = hashlib.sha256(b"secret-token-123").hexdigest()[:16]
    assert entry.unique_id == expected
    assert "secret-token-123" not in (entry.unique_id or "")


async def test_no_chargers_shows_error(hass):
    with _patched_client({"uuid": "u1"}, chargers=[]):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"token": "tok"}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "no_chargers_found"}


async def test_duplicate_account_aborts(hass):
    with _patched_client({"uuid": "dup"}):
        await _run_flow(hass, "tok")
    with _patched_client({"uuid": "dup"}):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"token": "tok2"}
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
