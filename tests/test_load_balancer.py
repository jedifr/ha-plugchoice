"""Tests du régulateur : détection du nombre réel de phases utilisées."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.plugchoice.load_balancer import PlugchoiceLoadBalancer, _ActiveCharger


def _lb() -> PlugchoiceLoadBalancer:
    return PlugchoiceLoadBalancer(
        MagicMock(), MagicMock(), MagicMock(), MagicMock(), AsyncMock(), set()
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
