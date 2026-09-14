"""Tests du régulateur : détection du nombre réel de phases utilisées."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.plugchoice.const import (
    CONF_GRID_POWER_ENTITY,
    CONF_LOAD_BALANCING_WINDOW,
    CONF_MAX_GRID_POWER,
    DEFAULT_CONNECTOR_ID,
)
from custom_components.plugchoice.load_balancer import PlugchoiceLoadBalancer, _ActiveCharger


def _lb(manual_current_overrides: dict | None = None) -> PlugchoiceLoadBalancer:
    return PlugchoiceLoadBalancer(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        MagicMock(),
        AsyncMock(),
        set(),
        manual_current_overrides if manual_current_overrides is not None else {},
    )


@pytest.mark.parametrize(
    ("meter", "profile", "expected"),
    [
        # Véhicule monophasé sur borne triphasée : L1 seul -> 1 phase
        ({"current_l1": 32, "current_l2": 0, "current_l3": 0}, {"number_phases": 3}, 1),
        ({"current_l1": 16, "current_l2": 16, "current_l3": 15}, {}, 3),
        ({"current_l1": 15, "current_l2": 14, "current_l3": 0}, {}, 2),
        # Bruit sous le seuil -> pas compté
        ({"current_l1": 30, "current_l2": 0.4, "current_l3": 0.1}, {}, 1),
        # Aucune mesure -> repli sur le profil OCPP
        ({}, {"number_phases": 1}, 1),
        # Ni mesure ni profil -> valeur par défaut
        ({}, None, 3),
    ],
)
def test_active_phase_count(meter, profile, expected):
    assert _lb()._active_phase_count(meter, profile) == expected


async def test_send_if_needed_refreshes_before_profile_expiry():
    """La même limite est réémise avant expiration (~3 min) du profil Plugchoice."""
    lb = _lb()
    lb._client.async_set_charging_limit = AsyncMock(return_value={"status": "Accepted"})

    # 1er envoi : vrai changement
    assert await lb._send_if_needed("c1", 16) is True
    assert lb._client.async_set_charging_limit.await_count == 1

    # même cible juste après : rien
    assert await lb._send_if_needed("c1", 16) is False
    assert lb._client.async_set_charging_limit.await_count == 1

    # profil sur le point d'expirer -> réémission (retourne False : pas un changement)
    lb._last_sent_at["c1"] -= 10_000
    assert await lb._send_if_needed("c1", 16) is False
    assert lb._client.async_set_charging_limit.await_count == 2

    # vrai changement de cible
    assert await lb._send_if_needed("c1", 24) is True
    assert lb._client.async_set_charging_limit.await_count == 3


async def test_send_if_needed_rejection_retries_next_cycle():
    lb = _lb()
    lb._client.async_set_charging_limit = AsyncMock(return_value={"status": "Rejected"})
    assert await lb._send_if_needed("c1", 16) is False
    assert "c1" not in lb._last_sent_at  # pas mémorisé -> retenté au cycle suivant
    assert await lb._send_if_needed("c1", 16) is False
    assert lb._client.async_set_charging_limit.await_count == 2


def test_single_phase_car_not_over_throttled():
    """Un véhicule monophasé doit pouvoir atteindre 32 A si le budget le permet."""
    lb = _lb()
    charger = _ActiveCharger(
        charger_id="c1",
        voltage=230.0,
        phases=1,  # mesuré : monophasé
        max_current=32.0,
        raw_max_current=32.0,
        priority=5,
        badge_id=None,
    )
    # 8 kW de budget : largement de quoi tenir 32 A en monophasé (~7.36 kW).
    targets = lb._distribute_budget([charger], 8000.0)
    amps = round(targets["c1"] / (charger.voltage * charger.phases))
    assert amps == 32

    # Avec l'ancienne hypothèse 3 phases, la même borne aurait été bridée :
    charger_3ph = _ActiveCharger(
        charger_id="c1", voltage=230.0, phases=3, max_current=32.0,
        raw_max_current=32.0, priority=5, badge_id=None,
    )
    amps_buggy = round(
        lb._distribute_budget([charger_3ph], 8000.0)["c1"] / (230.0 * 3)
    )
    assert amps_buggy < 32


def _active_charger_info(number_phases: int = 1) -> dict:
    return {
        "max_current": 32,
        "last_transaction": {"started_at": "2024-01-01T10:00:00Z", "stopped_at": None},
        "charging_profile": {"number_phases": number_phases},
    }


def _meter_coordinator(power: float, current_l1: float) -> MagicMock:
    meter = MagicMock()
    meter.data = {
        "power": power,
        "voltage_l1": 230.0,
        "current_l1": current_l1,
        "current_l2": 0.0,
        "current_l3": 0.0,
    }
    return meter


async def test_manual_override_exempts_from_budget_and_persists(hass):
    """Le réglage manuel est servi tel quel, pas la part calculée par le budget."""
    entry = MagicMock()
    entry.options = {
        CONF_GRID_POWER_ENTITY: "sensor.grid",
        CONF_MAX_GRID_POWER: 20000,
        CONF_LOAD_BALANCING_WINDOW: 60,
    }
    entry.entry_id = "entry1"

    chargers_coordinator = MagicMock()
    chargers_coordinator.data = {"c1": _active_charger_info()}
    chargers_coordinator.async_request_refresh = AsyncMock()

    ensure_meter_coordinator = AsyncMock(return_value=_meter_coordinator(3000.0, 13.0))
    client = MagicMock()
    client.async_set_charging_limit = AsyncMock(return_value={"status": "Accepted"})

    manual_overrides = {"c1": 10.0}
    lb = PlugchoiceLoadBalancer(
        hass, entry, client, chargers_coordinator, ensure_meter_coordinator,
        set(), manual_overrides,
    )
    lb._record_sample("5000")  # puissance réseau : le budget calculé serait très différent de 10A

    await lb._async_evaluate()

    client.async_set_charging_limit.assert_awaited_once_with("c1", DEFAULT_CONNECTOR_ID, 10)
    # Session toujours en cours (pas de stopped_at) -> le réglage manuel reste actif.
    assert manual_overrides == {"c1": 10.0}


async def test_manual_override_cleared_when_session_really_ends(hass):
    entry = MagicMock()
    entry.options = {
        CONF_GRID_POWER_ENTITY: "sensor.grid",
        CONF_MAX_GRID_POWER: 20000,
        CONF_LOAD_BALANCING_WINDOW: 60,
    }
    entry.entry_id = "entry1"

    ended_info = _active_charger_info()
    ended_info["last_transaction"]["stopped_at"] = "2024-01-01T11:00:00Z"  # session terminée
    chargers_coordinator = MagicMock()
    chargers_coordinator.data = {"c1": ended_info}
    chargers_coordinator.async_request_refresh = AsyncMock()

    # Puissance sous le seuil actif -> la borne n'est plus "active" ce cycle.
    ensure_meter_coordinator = AsyncMock(return_value=_meter_coordinator(0.0, 0.0))
    client = MagicMock()

    manual_overrides = {"c1": 10.0}
    lb = PlugchoiceLoadBalancer(
        hass, entry, client, chargers_coordinator, ensure_meter_coordinator,
        set(), manual_overrides,
    )
    lb._previously_active_ids = {"c1"}  # active au cycle précédent
    lb._record_sample("5000")

    await lb._async_evaluate()

    assert manual_overrides == {}
    chargers_coordinator.async_request_refresh.assert_awaited_once()


async def test_boost_takes_precedence_over_manual_override(hass):
    entry = MagicMock()
    entry.options = {
        CONF_GRID_POWER_ENTITY: "sensor.grid",
        CONF_MAX_GRID_POWER: 20000,
        CONF_LOAD_BALANCING_WINDOW: 60,
    }
    entry.entry_id = "entry1"

    chargers_coordinator = MagicMock()
    chargers_coordinator.data = {"c1": _active_charger_info()}
    chargers_coordinator.async_request_refresh = AsyncMock()

    ensure_meter_coordinator = AsyncMock(return_value=_meter_coordinator(3000.0, 13.0))
    client = MagicMock()
    client.async_set_charging_limit = AsyncMock(return_value={"status": "Accepted"})

    manual_overrides = {"c1": 10.0}
    lb = PlugchoiceLoadBalancer(
        hass, entry, client, chargers_coordinator, ensure_meter_coordinator,
        {"c1"}, manual_overrides,  # aussi boostée
    )
    lb._record_sample("5000")

    await lb._async_evaluate()

    # Boost l'emporte : la borne reçoit son max matériel (32A), pas les 10A du réglage manuel.
    client.async_set_charging_limit.assert_awaited_once_with("c1", DEFAULT_CONNECTOR_ID, 32)
