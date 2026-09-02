"""Fixtures communes aux tests Plugchoice."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Rend le custom_component chargeable par le harnais de test HA."""
    yield


@pytest.fixture
def api_client() -> MagicMock:
    """Client Plugchoice simulé, avec des réponses par défaut cohérentes."""
    client = MagicMock()
    client.async_get_user = AsyncMock(return_value={"uuid": "user-1", "name": "Compte test"})
    client.async_list_chargers = AsyncMock(return_value=[{"uuid": "c1", "reference": "Borne 1"}])
    client.async_get_plug_charge_status = AsyncMock(
        return_value={"current_card": "BADGE_A", "enabled": True}
    )
    client.async_get_lock_status = AsyncMock(
        return_value={"enabled": False, "interactable": True}
    )
    client.async_list_charger_transactions = AsyncMock(
        return_value=[
            {
                "id_tag": "BADGE_A",
                "total_kwh": 5.0,
                "started_at": "2024-01-01T10:00:00Z",
                "stopped_at": "2024-01-01T12:00:00Z",
            }
        ]
    )
    client.async_get_latest_charging_profile = AsyncMock(
        return_value={"limit": 16, "charging_rate_unit": "A", "number_phases": 3}
    )
    client.async_list_sites = AsyncMock(return_value=[{"uuid": "s1"}])
    client.async_list_cards = AsyncMock(
        return_value=[{"id_token": "BADGE_A", "name": "Alice"}]
    )
    return client
