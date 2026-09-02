"""Tests des coordinators (enrichissement parallèle, cliquet énergie, intervalle adaptatif)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.plugchoice.api import PlugchoiceApiError
from custom_components.plugchoice.coordinator import (
    PlugchoiceBadgeEnergyCoordinator,
    PlugchoiceChargersCoordinator,
    PlugchoiceMeterCoordinator,
    connector_error_code,
    connector_status,
)


@pytest.mark.parametrize(
    ("charger_info", "expected"),
    [
        ({"_detail": {"connectors": [{"status": "Available"}]}}, "Available"),
        ({"connectors": [{"state": "Charging"}]}, "Charging"),
        ({"_detail": {"connector": {"status": "SuspendedEV"}}}, "SuspendedEV"),
        ({"_detail": {"status": "Preparing"}}, "Preparing"),
        ({"_detail": {"connectors": []}}, None),
        ({}, None),
    ],
)
def test_connector_status_parsing(charger_info, expected):
    assert connector_status(charger_info) == expected


def test_connector_error_code_parsing():
    assert connector_error_code(
        {"_detail": {"connectors": [{"status": "Faulted", "error_code": "GroundFailure"}]}}
    ) == "GroundFailure"
    assert connector_error_code({}) is None


async def test_chargers_enrichment_ok(hass, api_client):
    coordinator = PlugchoiceChargersCoordinator(hass, api_client)
    data = await coordinator._async_update_data()

    charger = data["c1"]
    assert charger["current_card"] == "BADGE_A"
    assert charger["lock_enabled"] is False
    assert charger["charging_profile"]["limit"] == 16
    # La liste de transactions est conservée pour le coordinator d'énergie.
    assert charger["transactions"]
    assert charger["last_completed_transaction"]["total_kwh"] == 5.0
    assert coordinator.badge_directory == {"BADGE_A": "Alice"}
    # Objet borne complet -> statut du connecteur exploitable
    assert connector_status(charger) == "Available"


async def test_chargers_enrichment_partial_failure(hass, api_client):
    """Une section en erreur ne doit pas empêcher les autres de se remplir."""
    api_client.async_get_lock_status = AsyncMock(side_effect=PlugchoiceApiError("boom"))
    coordinator = PlugchoiceChargersCoordinator(hass, api_client)
    data = await coordinator._async_update_data()

    assert "lock_enabled" not in data["c1"]
    assert data["c1"]["current_card"] == "BADGE_A"  # les autres appels passent


async def test_chargers_badge_directory_preserved_on_card_failure(hass, api_client):
    api_client.async_list_cards = AsyncMock(side_effect=PlugchoiceApiError("403"))
    coordinator = PlugchoiceChargersCoordinator(hass, api_client)
    coordinator.badge_directory = {"BADGE_A": "Alice (connu)"}
    await coordinator._async_update_data()
    assert coordinator.badge_directory == {"BADGE_A": "Alice (connu)"}


def _chargers_stub(**transactions_by_charger):
    stub = MagicMock()
    stub.data = {
        cid: {"transactions": txs} for cid, txs in transactions_by_charger.items()
    }
    return stub


async def test_badge_energy_sums_transactions(hass):
    chargers = _chargers_stub(
        c1=[{"id_tag": "A", "total_kwh": "5.0"}, {"id_tag": "A", "total_kwh": 3.0}],
        c2=[{"id_tag": "B", "total_kwh": 2.0}],
    )
    coordinator = PlugchoiceBadgeEnergyCoordinator(hass, chargers)
    assert await coordinator._async_update_data() == {"A": 8.0, "B": 2.0}


async def test_badge_energy_never_decreases(hass):
    chargers = _chargers_stub(
        c1=[{"id_tag": "A", "total_kwh": 8.0}],
        c2=[{"id_tag": "B", "total_kwh": 2.0}],
    )
    coordinator = PlugchoiceBadgeEnergyCoordinator(hass, chargers)
    await coordinator._async_update_data()

    # c2 en erreur au cycle suivant : plus de clé "transactions".
    del chargers.data["c2"]["transactions"]
    result = await coordinator._async_update_data()
    assert result["B"] == 2.0  # pas de retour à zéro -> pas de faux "reset"

    # c2 revient avec une session de plus.
    chargers.data["c2"]["transactions"] = [
        {"id_tag": "B", "total_kwh": 2.0},
        {"id_tag": "B", "total_kwh": 4.0},
    ]
    result = await coordinator._async_update_data()
    assert result["B"] == 6.0


async def test_badge_energy_all_failed_raises(hass):
    chargers = _chargers_stub(c1=[{"id_tag": "A", "total_kwh": 1.0}])
    coordinator = PlugchoiceBadgeEnergyCoordinator(hass, chargers)
    del chargers.data["c1"]["transactions"]
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


def _meter_client(power_value):
    client = MagicMock()
    values = []
    if power_value is not None:
        values.append(
            {
                "measurand": "Power.Active.Import",
                "phase": None,
                "value": str(power_value),
                "timestamp": "2024-01-01T10:00:00Z",
            }
        )
    client.async_get_latest_meter_values = AsyncMock(return_value=values)
    return client


async def test_meter_interval_active(hass):
    coordinator = PlugchoiceMeterCoordinator(hass, _meter_client(3000), "c1", 60)
    await coordinator._async_update_data()
    assert coordinator.update_interval.total_seconds() == 60


@pytest.mark.parametrize("power", [0, 150, None])
async def test_meter_interval_idle(hass, power):
    coordinator = PlugchoiceMeterCoordinator(hass, _meter_client(power), "c1", 60)
    await coordinator._async_update_data()
    assert coordinator.update_interval.total_seconds() == 300
