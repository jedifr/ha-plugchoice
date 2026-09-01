"""Entités button Plugchoice : démarrer/arrêter une session de charge à distance.

L'API exige un id_token (badge RFID) pour démarrer une session. Le bouton
utilise, dans l'ordre : le badge choisi via l'entité select "Badge pour
démarrage" de la borne, puis le badge configuré en Plug & Charge
(current_card), puis en dernier recours un jeton générique — voir
DEFAULT_ID_TOKEN ci-dessous, qui ne fonctionnera que si la borne accepte
l'auto-démarrage sans badge enregistré ou si ce jeton y est explicitement
autorisé.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import PlugchoiceApiError, PlugchoiceClient
from .const import DOMAIN
from .coordinator import PlugchoiceChargersCoordinator

_LOGGER = logging.getLogger(__name__)

# Dernier recours si aucun badge n'est sélectionné ni configuré. Ne
# fonctionnera que si la borne/Plugchoice accepte un id_token non
# préalablement enregistré (dépend de la config d'autorisation du site) —
# utilise plutôt l'entité select "Badge pour démarrage" de la borne.
DEFAULT_ID_TOKEN = "HOMEASSISTANT"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Crée les boutons démarrer/arrêter pour chaque borne déjà découverte, et les suivantes."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    chargers_coordinator: PlugchoiceChargersCoordinator = domain_data["chargers_coordinator"]
    client = domain_data["client"]
    selected_start_badge: dict[str, str] = domain_data["selected_start_badge"]

    known_charger_ids: set[str] = set()

    def _display_name(charger_id: str, charger_info: dict[str, Any]) -> str:
        return (
            charger_info.get("reference")
            or charger_info.get("identity")
            or f"Borne {charger_id[:8]}"
        )

    def _add_entities_for_charger(charger_id: str, charger_info: dict[str, Any]) -> None:
        name = _display_name(charger_id, charger_info)
        async_add_entities(
            [
                PlugchoiceStartChargingButton(
                    chargers_coordinator, client, charger_id, name, selected_start_badge
                ),
                PlugchoiceStopChargingButton(chargers_coordinator, client, charger_id, name),
            ]
        )
        known_charger_ids.add(charger_id)

    for charger_id, charger_info in chargers_coordinator.data.items():
        _add_entities_for_charger(charger_id, charger_info)

    def _handle_chargers_update() -> None:
        for charger_id, charger_info in chargers_coordinator.data.items():
            if charger_id in known_charger_ids:
                continue
            _add_entities_for_charger(charger_id, charger_info)

    entry.async_on_unload(
        chargers_coordinator.async_add_listener(_handle_chargers_update)
    )


class _PlugchoiceActionButton(CoordinatorEntity[PlugchoiceChargersCoordinator], ButtonEntity):
    """Base commune aux boutons d'action Plugchoice."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PlugchoiceChargersCoordinator,
        client: PlugchoiceClient,
        charger_id: str,
        device_name: str,
        unique_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._charger_id = charger_id
        self._attr_unique_id = f"{charger_id}_{unique_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger_id)},
            name=device_name,
            manufacturer="Plugchoice",
            model="Borne de recharge",
            configuration_url=f"https://app.plugchoice.com/chargers/{charger_id}",
        )

    def _charger_info(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get(self._charger_id) or {}


class PlugchoiceStartChargingButton(_PlugchoiceActionButton):
    """Démarre une session de charge à distance."""

    _attr_translation_key = "start_charging"
    _attr_name = "Démarrer la charge"
    _attr_icon = "mdi:play-circle-outline"

    def __init__(
        self,
        coordinator: PlugchoiceChargersCoordinator,
        client: PlugchoiceClient,
        charger_id: str,
        device_name: str,
        selected_start_badge: dict[str, str],
    ) -> None:
        super().__init__(coordinator, client, charger_id, device_name, unique_suffix="start_charging")
        self._selected_start_badge = selected_start_badge

    async def async_press(self) -> None:
        id_token = (
            self._selected_start_badge.get(self._charger_id)
            or self._charger_info().get("current_card")
            or DEFAULT_ID_TOKEN
        )
        if id_token == DEFAULT_ID_TOKEN:
            _LOGGER.warning(
                "Aucun badge sélectionné pour la borne %s : envoi du jeton générique "
                "'%s', qui a de bonnes chances d'être refusé par Plugchoice. "
                "Choisis un badge via l'entité 'Badge pour démarrage' de cette borne.",
                self._charger_id,
                DEFAULT_ID_TOKEN,
            )
        try:
            result = await self._client.async_start_charging(self._charger_id, id_token)
        except PlugchoiceApiError as err:
            _LOGGER.error(
                "Échec du démarrage de la charge sur %s (id_token=%s): %s",
                self._charger_id,
                id_token,
                err,
            )
            raise

        status = str((result or {}).get("status") or "").lower()
        if status and status not in ("accepted", "ok", "success"):
            raise HomeAssistantError(
                f"La borne a refusé le démarrage avec le badge {id_token} "
                f"(statut: {status}). Vérifie que ce badge est bien autorisé sur ce site "
                "Plugchoice, ou choisis-en un autre via l'entité 'Badge pour démarrage'."
            )

        await self.coordinator.async_request_refresh()


class PlugchoiceStopChargingButton(_PlugchoiceActionButton):
    """Arrête la session de charge en cours."""

    _attr_translation_key = "stop_charging"
    _attr_name = "Arrêter la charge"
    _attr_icon = "mdi:stop-circle-outline"

    def __init__(self, *args: Any) -> None:
        super().__init__(*args, unique_suffix="stop_charging")

    async def async_press(self) -> None:
        try:
            await self._client.async_stop_charging(self._charger_id)
        except PlugchoiceApiError as err:
            _LOGGER.error("Échec de l'arrêt de la charge sur %s: %s", self._charger_id, err)
            raise
        await self.coordinator.async_request_refresh()
