"""Tests des entités sensibles à l'unité A/W du profil de charge."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.plugchoice.button import PlugchoiceClearLimitButton
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
    entity = PlugchoiceChargingLimitNumber(_coordinator(profile), MagicMock(), "c1", "Borne 1")
    assert entity._current_known_limit() == expected


def test_number_native_value_falls_back_when_watts():
    entity = PlugchoiceChargingLimitNumber(
        _coordinator({"limit": 7000, "charging_rate_unit": "W"}), MagicMock(), "c1", "Borne 1"
    )
    # Aucune valeur en A connue et aucune valeur optimiste envoyée -> None,
    # surtout pas 7000 affiché comme des ampères sur le slider.
    assert entity.native_value is None


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


async def test_clear_limit_button_raises_when_rejected():
    coordinator = _coordinator(None)
    coordinator.async_request_refresh = AsyncMock()
    client = MagicMock()
    client.async_clear_charging_limit = AsyncMock(return_value={"status": "Rejected"})

    button = PlugchoiceClearLimitButton(coordinator, client, "c1", "Borne 1")
    with pytest.raises(Exception):
        await button.async_press()
