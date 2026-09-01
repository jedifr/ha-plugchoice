"""Entité number Plugchoice : réglage à distance de la limite de charge (A).

Envoie une action "charge-limit" à la borne via l'API Plugchoice quand la
valeur est modifiée dans Home Assistant.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import PlugchoiceApiError, PlugchoiceClient
from .const import (
    DEFAULT_CONNECTOR_ID,
    DEFAULT_MAX_CHARGING_CURRENT,
    DOMAIN,
    MIN_CHARGING_CURRENT,
)
from .coordinator import PlugchoiceChargersCoordinator

_LOGGER = logging.getLogger(__name__)

# Repli raisonnable si max_current n'est pas connu côté Plugchoice pour la
# borne (cf. schéma : ce champ peut être null tant que la borne n'a jamais
# communiqué). 32A est la limite haute usuelle pour une prise domestique
# triphasée ; ajustable via l'attribut max de l'entité si besoin.
DEFAULT_MAX_CURRENT = DEFAULT_MAX_CHARGING_CURRENT
# Minimum usuel côté OCPP en dessous duquel la plupart des véhicules
# n'acceptent plus de charger.
MIN_CURRENT = MIN_CHARGING_CURRENT

# On suppose le connecteur 1 par défaut (cas le plus courant : une seule
# prise par borne). Pour une borne multi-connecteurs, ce number pilote
# uniquement le premier connecteur.


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Crée un number "Limite de charge" pour chaque borne déjà découverte, et les suivantes."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    chargers_coordinator: PlugchoiceChargersCoordinator = domain_data["chargers_coordinator"]
    client = domain_data["client"]

    known_charger_ids: set[str] = set()

    def _display_name(charger_id: str, charger_info: dict[str, Any]) -> str:
        return (
            charger_info.get("reference")
            or charger_info.get("identity")
            or f"Borne {charger_id[:8]}"
        )

    def _add_entity_for_charger(charger_id: str, charger_info: dict[str, Any]) -> None:
        async_add_entities(
            [
                PlugchoiceChargingLimitNumber(
                    chargers_coordinator,
                    client,
                    charger_id,
                    _display_name(charger_id, charger_info),
                )
            ]
        )
        known_charger_ids.add(charger_id)

    for charger_id, charger_info in chargers_coordinator.data.items():
        _add_entity_for_charger(charger_id, charger_info)

    def _handle_chargers_update() -> None:
        for charger_id, charger_info in chargers_coordinator.data.items():
            if charger_id in known_charger_ids:
                continue
            _add_entity_for_charger(charger_id, charger_info)

    entry.async_on_unload(
        chargers_coordinator.async_add_listener(_handle_chargers_update)
    )


class PlugchoiceChargingLimitNumber(
    CoordinatorEntity[PlugchoiceChargersCoordinator], NumberEntity
):
    """Permet de définir la limite de charge (A) d'une borne depuis HA.

    C'est une entité de commande, pas de lecture : sa valeur affichée est
    la dernière valeur envoyée depuis HA (pas nécessairement synchronisée
    avec le profil réellement actif si celui-ci a été changé depuis le
    portail Plugchoice ou l'app — consulter le capteur "Profil de charge
    actif" pour la valeur réellement appliquée par la borne).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "charging_limit"
    _attr_name = "Limite de charge"
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_min_value = MIN_CURRENT
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:current-ac"

    def __init__(
        self,
        coordinator: PlugchoiceChargersCoordinator,
        client: PlugchoiceClient,
        charger_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._charger_id = charger_id
        self._attr_unique_id = f"{charger_id}_charging_limit"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger_id)},
            name=device_name,
            manufacturer="Plugchoice",
            model="Borne de recharge",
            configuration_url=f"https://app.plugchoice.com/chargers/{charger_id}",
        )
        # Repli affiché tant qu'aucun profil de charge n'a jamais été
        # observé côté coordinator (ex: juste après ajout de la borne).
        self._fallback_value: float | None = None

    def _current_known_limit(self) -> float | None:
        charger_info = (self.coordinator.data or {}).get(self._charger_id) or {}
        profile = charger_info.get("charging_profile") or {}
        limit = profile.get("limit")
        # Ce slider est en ampères : un profil exprimé en W (unité OCPP
        # possible) n'est pas comparable et ne doit pas être affiché ici
        # (le capteur "Profil de charge actif" le montre avec son unité).
        unit = str(profile.get("charging_rate_unit") or "A").upper()
        if unit not in ("A", "AMPERE", "AMPERES"):
            return None
        try:
            return float(limit) if limit is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def native_value(self) -> float | None:
        """Toujours dérivé des données live du coordinator quand disponibles.

        Comme cette entité hérite de CoordinatorEntity, chaque
        rafraîchissement de chargers_coordinator (découverte périodique,
        ou déclenché manuellement après une commande) met automatiquement
        à jour l'affichage — y compris si la limite a été changée par le
        load balancer, le portail Plugchoice ou l'app, pas seulement par
        ce slider.
        """
        live = self._current_known_limit()
        return live if live is not None else self._fallback_value

    @property
    def native_max_value(self) -> float:
        charger_info = (self.coordinator.data or {}).get(self._charger_id) or {}
        max_current = charger_info.get("max_current")
        try:
            return float(max_current) if max_current else DEFAULT_MAX_CURRENT
        except (TypeError, ValueError):
            return DEFAULT_MAX_CURRENT

    async def async_set_native_value(self, value: float) -> None:
        """Envoie la nouvelle limite à la borne via l'API Plugchoice."""
        try:
            await self._client.async_set_charging_limit(
                self._charger_id, DEFAULT_CONNECTOR_ID, value
            )
        except PlugchoiceApiError as err:
            _LOGGER.error(
                "Échec de l'envoi de la limite de charge (%s A) à la borne %s: %s",
                value,
                self._charger_id,
                err,
            )
            raise
        # Affichage optimiste immédiat, remplacé dès que le coordinator se
        # rafraîchit par la valeur réellement confirmée par la borne.
        self._fallback_value = value
        self.async_write_ha_state()
        # Rafraîchit le profil de charge peu après pour refléter la
        # confirmation (ou le rejet) de la borne dans le capteur diagnostic.
        await self.coordinator.async_request_refresh()
