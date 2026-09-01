"""Entité switch Plugchoice : mode "Boost" manuel par borne.

À l'activation, envoie immédiatement la limite de charge maximale de la
borne via l'action "charge-limit" — indépendamment du load balancing
(fonctionne même si celui-ci n'est pas configuré/activé). La borne est
aussi ajoutée à l'ensemble partagé "boosted_chargers", pour que le load
balancer (si actif) l'exempte du partage de budget sur ses cycles suivants
plutôt que de la ramener vers son minimum garanti.

À la désactivation, restaure la limite qui était en vigueur juste avant
l'activation du Boost (si connue), là encore indépendamment du load
balancing — qui, s'il est actif, recalculera de toute façon une valeur
adaptée dans les 15 secondes qui suivent.

L'état est gardé en mémoire uniquement (pas persisté sur disque) : il
repasse à "désactivé" au redémarrage de Home Assistant, par sécurité — un
boost oublié ne doit pas rester actif indéfiniment après un redémarrage.
Il se désactive aussi automatiquement dès que la session de charge en
cours se termine (détecté par load_balancer.py), pour ne jamais profiter
par erreur à la session suivante sur la même borne.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import PlugchoiceApiError, PlugchoiceClient
from .const import DEFAULT_CONNECTOR_ID, DEFAULT_MAX_CHARGING_CURRENT, DOMAIN
from .coordinator import PlugchoiceChargersCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Crée un switch "Boost" par borne déjà découverte, et les suivantes."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    chargers_coordinator: PlugchoiceChargersCoordinator = domain_data["chargers_coordinator"]
    client = domain_data["client"]
    boosted_chargers: set[str] = domain_data["boosted_chargers"]

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
                PlugchoiceBoostSwitch(
                    chargers_coordinator,
                    client,
                    boosted_chargers,
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


class PlugchoiceBoostSwitch(CoordinatorEntity[PlugchoiceChargersCoordinator], SwitchEntity):
    """Active/désactive le mode Boost (puissance maximale, budget ignoré) pour une borne."""

    _attr_has_entity_name = True
    _attr_translation_key = "charger_boost"
    _attr_name = "Boost"
    _attr_icon = "mdi:rocket-launch-outline"

    def __init__(
        self,
        coordinator: PlugchoiceChargersCoordinator,
        client: PlugchoiceClient,
        boosted_chargers: set[str],
        charger_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._boosted_chargers = boosted_chargers
        self._charger_id = charger_id
        self._attr_unique_id = f"{charger_id}_boost"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger_id)},
            name=device_name,
            manufacturer="Plugchoice",
            model="Borne de recharge",
            configuration_url=f"https://app.plugchoice.com/chargers/{charger_id}",
        )
        # Limite connue juste avant l'activation du Boost, pour la
        # restaurer à la désactivation. En mémoire uniquement : si HA
        # redémarre pendant un Boost actif, rien à restaurer (cohérent
        # avec la remise à zéro du Boost lui-même au redémarrage).
        self._pre_boost_limit: float | None = None

    def _charger_info(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._charger_id) or {}

    def _max_current(self) -> float:
        max_current = self._charger_info().get("max_current")
        try:
            return float(max_current) if max_current else DEFAULT_MAX_CHARGING_CURRENT
        except (TypeError, ValueError):
            return DEFAULT_MAX_CHARGING_CURRENT

    def _current_known_limit(self) -> float | None:
        profile = self._charger_info().get("charging_profile") or {}
        limit = profile.get("limit")
        # Restauration post-Boost en ampères uniquement : on ignore un
        # profil exprimé en W (sinon on renverrait une valeur en watts
        # interprétée comme des ampères par l'action charge-limit).
        unit = str(profile.get("charging_rate_unit") or "A").upper()
        if unit not in ("A", "AMPERE", "AMPERES"):
            return None
        try:
            return float(limit) if limit is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def is_on(self) -> bool:
        return self._charger_id in self._boosted_chargers

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._pre_boost_limit = self._current_known_limit()
        self._boosted_chargers.add(self._charger_id)
        self.async_write_ha_state()

        max_current = self._max_current()
        _LOGGER.debug(
            "Boost: activation sur %s — envoi de la limite max %sA (max matériel connu)",
            self._charger_id,
            max_current,
        )
        try:
            result = await self._client.async_set_charging_limit(
                self._charger_id, DEFAULT_CONNECTOR_ID, max_current
            )
        except PlugchoiceApiError as err:
            _LOGGER.error(
                "Échec de l'envoi de la limite max (%sA) au démarrage du Boost sur %s: %s",
                max_current,
                self._charger_id,
                err,
            )
            raise

        status = str((result or {}).get("status") or "").lower()
        _LOGGER.debug(
            "Boost: réponse de la borne %s pour %sA — statut=%s, corps=%s",
            self._charger_id,
            max_current,
            status or "(vide)",
            result,
        )
        if status and status not in ("accepted", "ok", "success"):
            raise HomeAssistantError(
                f"La borne a refusé la limite max ({max_current}A) demandée par le Boost "
                f"(statut: {status})."
            )

        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._boosted_chargers.discard(self._charger_id)
        self.async_write_ha_state()

        target = self._pre_boost_limit
        self._pre_boost_limit = None
        if target is None:
            # Rien à restaurer (jamais connu) : si le load balancing est
            # actif, son prochain cycle (15s) recalculera une valeur
            # adaptée de toute façon.
            return

        try:
            await self._client.async_set_charging_limit(
                self._charger_id, DEFAULT_CONNECTOR_ID, target
            )
        except PlugchoiceApiError as err:
            _LOGGER.warning(
                "Échec de la restauration de la limite (%sA) après désactivation du Boost sur %s: %s",
                target,
                self._charger_id,
                err,
            )
            return
        await self.coordinator.async_request_refresh()
