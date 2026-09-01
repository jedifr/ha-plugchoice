"""Entité lock Plugchoice : verrouille/déverrouille la borne à distance.

Une borne verrouillée passe "Unavailable" côté OCPP et refuse toute
nouvelle session — c'est la fonction "Verrouiller la borne" de l'app
Plugchoice (protection contre l'usage non désiré), distincte du verrou de
câble physique ("Socket Lock", non exposé par cette intégration).
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import PlugchoiceApiError, PlugchoiceClient
from .const import DOMAIN
from .coordinator import PlugchoiceChargersCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Crée un lock par borne déjà découverte, et les suivantes."""
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
                PlugchoiceLock(
                    chargers_coordinator, client, charger_id, _display_name(charger_id, charger_info)
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


class PlugchoiceLock(CoordinatorEntity[PlugchoiceChargersCoordinator], LockEntity):
    """Verrou "borne disponible / indisponible" côté OCPP."""

    _attr_has_entity_name = True
    _attr_translation_key = "charger_lock"
    _attr_name = "Verrouillage"
    _attr_icon = "mdi:lock-outline"

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
        self._attr_unique_id = f"{charger_id}_lock"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger_id)},
            name=device_name,
            manufacturer="Plugchoice",
            model="Borne de recharge",
            configuration_url=f"https://app.plugchoice.com/chargers/{charger_id}",
        )

    def _charger_info(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._charger_id) or {}

    @property
    def is_locked(self) -> bool | None:
        return self._charger_info().get("lock_enabled")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # "interactable" indique si Plugchoice acceptera le changement
        # maintenant (false pendant une session active, par ex.).
        return {"interactable": self._charger_info().get("lock_interactable")}

    async def async_lock(self, **kwargs: Any) -> None:
        await self._set_lock(True)

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._set_lock(False)

    async def _set_lock(self, enabled: bool) -> None:
        try:
            await self._client.async_set_lock(self._charger_id, enabled)
        except PlugchoiceApiError as err:
            _LOGGER.error(
                "Échec du %s de la borne %s: %s",
                "verrouillage" if enabled else "déverrouillage",
                self._charger_id,
                err,
            )
            raise
        await self.coordinator.async_request_refresh()
