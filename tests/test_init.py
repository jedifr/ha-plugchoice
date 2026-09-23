"""Tests du nettoyage des appareils orphelins (bornes disparues côté Plugchoice)."""
from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.plugchoice import _async_prune_stale_charger_devices
from custom_components.plugchoice.const import DOMAIN


async def test_prune_removes_only_devices_for_disappeared_chargers(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)

    stale_charger = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "charger-gone")},
        name="Borne disparue",
    )
    current_charger = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "charger-current")},
        name="Borne actuelle",
    )
    badge_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "badge_A1B2")},
        name="Badge",
    )
    load_balancing_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_load_balancing")},
        name="Répartition de puissance",
    )

    chargers_coordinator = MagicMock()
    chargers_coordinator.data = {"charger-current": {}}

    await _async_prune_stale_charger_devices(hass, entry, chargers_coordinator)

    assert device_registry.async_get(stale_charger.id) is None
    assert device_registry.async_get(current_charger.id) is not None
    assert device_registry.async_get(badge_device.id) is not None
    assert device_registry.async_get(load_balancing_device.id) is not None


async def test_prune_is_a_noop_when_nothing_disappeared(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "charger-current")},
        name="Borne actuelle",
    )

    chargers_coordinator = MagicMock()
    chargers_coordinator.data = {"charger-current": {}}

    await _async_prune_stale_charger_devices(hass, entry, chargers_coordinator)

    assert device_registry.async_get(device.id) is not None
