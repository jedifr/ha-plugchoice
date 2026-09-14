"""Tests des entités sensibles à l'unité A/W du profil de charge."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.plugchoice.button import (
    PlugchoiceClearLimitButton,
    PlugchoiceStartChargingButton,
)
from custom_components.plugchoice.number import PlugchoiceChargingLimitNumber
from custom_components.plugchoice.switch import PlugchoiceBoostSwitch


def _coordinator(profile: dict | None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = {"c1": {"charging_profile": profile, "max_current": 32}}
    return coordinator


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ({"limit": 16, "charging_rate_unit": "A"}, 16.0),
        ({"limit": 10}, 10.0),  # unité absente -> supposée ampères
        ({"limit": 7000, "charging_rate_unit": "W"}, None),  # watts : ignoré
        ({"limit": 7000, "charging_rate_unit": "w"}, None),
        ({}, None),
    ],
)
def test_number_current_known_limit_unit_filter(profile, expected):
    entity = PlugchoiceChargingLimitNumber(
        _coordinator(profile), MagicMock(), "c1", "Borne 1", {}
    )
    assert entity._current_known_limit() == expected


def test_number_native_value_falls_back_when_watts():
    entity = PlugchoiceChargingLimitNumber(
        _coordinator({"limit": 7000, "charging_rate_unit": "W"}), MagicMock(), "c1", "Borne 1", {}
    )
    # Aucune valeur en A connue et aucune valeur optimiste envoyée -> None,
    # surtout pas 7000 affiché comme des ampères sur le slider.
    assert entity.native_value is None


async def test_number_set_value_registers_manual_override():
    """Un changement du slider enregistre l'override lu par le load balancer."""
    coordinator = _coordinator({"limit": 6, "charging_rate_unit": "A"})
    coordinator.async_request_refresh = AsyncMock()
    client = MagicMock()
    client.async_set_charging_limit = AsyncMock(return_value={"status": "Accepted"})
    overrides: dict = {}
    entity = PlugchoiceChargingLimitNumber(coordinator, client, "c1", "Borne 1", overrides)
    # Entité non ajoutée à hass dans ce test unitaire : on n'exerce pas la
    # vraie écriture d'état, seulement l'enregistrement de l'override.
    entity.async_write_ha_state = MagicMock()

    await entity.async_set_native_value(24)

    assert overrides == {"c1": 24}
    assert entity.extra_state_attributes == {"load_balancing_override_active": True}


async def test_number_native_value_survives_stale_readback_right_after_set(monkeypatch):
    """La valeur envoyée ne doit pas "retomber" si le coordinator relit encore l'ancien profil.

    Reproduit le bug rapporté : juste après avoir monté le slider, le
    rafraîchissement déclenché par async_set_native_value arrive souvent
    avant que Plugchoice n'ait indexé le nouveau profil -> sans sursis
    optimiste, native_value republiait l'ancienne valeur (basse), donnant
    l'impression que le slider ne progressait que par petits paliers.
    """
    coordinator = _coordinator({"limit": 6, "charging_rate_unit": "A"})  # ancien profil, pas encore à jour
    coordinator.async_request_refresh = AsyncMock()
    client = MagicMock()
    client.async_set_charging_limit = AsyncMock(return_value={"status": "Accepted"})
    entity = PlugchoiceChargingLimitNumber(coordinator, client, "c1", "Borne 1", {})
    entity.async_write_ha_state = MagicMock()

    await entity.async_set_native_value(32)

    # Même si le coordinator (mocké) continue de relire l'ancien profil
    # (6 A), l'entité affiche la valeur demandée juste après l'envoi.
    assert coordinator.data["c1"]["charging_profile"]["limit"] == 6
    assert entity.native_value == 32

    # Une fois le sursis expiré, elle repasse en lecture live (ici toujours
    # l'ancien profil puisque le mock ne change pas -> démontre juste que le
    # sursis n'est pas permanent).
    entity._fallback_set_at -= 999
    assert entity.native_value == 6.0


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ({"limit": 20, "charging_rate_unit": "A"}, 20.0),
        ({"limit": 11000, "charging_rate_unit": "W"}, None),
    ],
)
def test_switch_pre_boost_limit_unit_filter(profile, expected):
    switch = PlugchoiceBoostSwitch(_coordinator(profile), MagicMock(), set(), "c1", "Borne 1")
    assert switch._current_known_limit() == expected


async def test_clear_limit_button_calls_api_and_refreshes():
    coordinator = _coordinator({"limit": 16, "charging_rate_unit": "A"})
    coordinator.async_request_refresh = AsyncMock()
    client = MagicMock()
    client.async_clear_charging_limit = AsyncMock(return_value={"status": "Accepted"})

    button = PlugchoiceClearLimitButton(coordinator, client, "c1", "Borne 1")
    await button.async_press()

    client.async_clear_charging_limit.assert_awaited_once_with("c1", 1)
    coordinator.async_request_refresh.assert_awaited_once()


def _start_button(charger_info: dict) -> PlugchoiceStartChargingButton:
    coordinator = MagicMock()
    coordinator.data = {"c1": charger_info}
    coordinator.async_request_refresh = AsyncMock()
    client = MagicMock()
    client.async_start_charging = AsyncMock(return_value={"status": "Accepted"})
    return PlugchoiceStartChargingButton(
        coordinator, client, "c1", "Borne 1", {"c1": "BADGE_A"}
    )


async def test_start_button_blocks_when_session_in_progress():
    button = _start_button(
        {"last_transaction": {"started_at": "2024-01-01T10:00:00Z", "stopped_at": None}}
    )
    with pytest.raises(HomeAssistantError, match="session est déjà en cours"):
        await button.async_press()
    button._client.async_start_charging.assert_not_awaited()


async def test_start_button_blocks_when_connector_suspended():
    button = _start_button({"_detail": {"connectors": [{"status": "SuspendedEV"}]}})
    with pytest.raises(HomeAssistantError, match="pas prête"):
        await button.async_press()
    button._client.async_start_charging.assert_not_awaited()


async def test_start_button_proceeds_when_available():
    button = _start_button({"_detail": {"connectors": [{"status": "Available"}]}})
    await button.async_press()
    button._client.async_start_charging.assert_awaited_once_with("c1", "BADGE_A")


async def test_clear_limit_button_raises_when_rejected():
    coordinator = _coordinator(None)
    coordinator.async_request_refresh = AsyncMock()
    client = MagicMock()
    client.async_clear_charging_limit = AsyncMock(return_value={"status": "Rejected"})

    button = PlugchoiceClearLimitButton(coordinator, client, "c1", "Borne 1")
    with pytest.raises(Exception):
        await button.async_press()
