"""Entité select Plugchoice : choix du badge (id_token) pour démarrer une charge.

L'action "Démarrer la charge" exige un id_token (badge RFID) pour
autoriser la session — voir api.py:async_start_charging. Ce sélecteur
liste tous les badges déjà connus de l'intégration (cartes enregistrées
sur Plugchoice, badges vus dans l'historique des transactions, noms
surchargés manuellement) pour que l'utilisateur choisisse lequel utiliser,
plutôt que de deviner un id_token à la main.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BADGE_NAMES, DOMAIN
from .coordinator import PlugchoiceBadgeEnergyCoordinator, PlugchoiceChargersCoordinator

NO_BADGE_OPTION = "(aucun badge sélectionné)"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Crée un select "Badge pour démarrage" par borne déjà découverte, et les suivantes."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    chargers_coordinator: PlugchoiceChargersCoordinator = domain_data["chargers_coordinator"]
    badge_energy_coordinator: PlugchoiceBadgeEnergyCoordinator = domain_data[
        "badge_energy_coordinator"
    ]
    badge_names: dict[str, str] = entry.options.get(CONF_BADGE_NAMES, {})
    selected_start_badge: dict[str, str] = domain_data["selected_start_badge"]

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
                PlugchoiceStartBadgeSelect(
                    chargers_coordinator,
                    badge_energy_coordinator,
                    badge_names,
                    selected_start_badge,
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


class PlugchoiceStartBadgeSelect(CoordinatorEntity[PlugchoiceChargersCoordinator], SelectEntity):
    """Choix du badge à utiliser pour le prochain démarrage à distance de cette borne."""

    _attr_has_entity_name = True
    _attr_translation_key = "start_badge"
    _attr_name = "Badge pour démarrage"
    _attr_icon = "mdi:credit-card-search-outline"

    def __init__(
        self,
        coordinator: PlugchoiceChargersCoordinator,
        badge_energy_coordinator: PlugchoiceBadgeEnergyCoordinator,
        badge_names: dict[str, str],
        selected_start_badge: dict[str, str],
        charger_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._badge_energy_coordinator = badge_energy_coordinator
        self._badge_names = badge_names
        self._selected_start_badge = selected_start_badge
        self._charger_id = charger_id
        self._id_by_label: dict[str, str] = {}
        self._attr_unique_id = f"{charger_id}_start_badge"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger_id)},
            name=device_name,
            manufacturer="Plugchoice",
            model="Borne de recharge",
            configuration_url=f"https://app.plugchoice.com/chargers/{charger_id}",
        )

        # Par défaut : le badge Plug & Charge déjà configuré sur la borne,
        # si connu, plutôt que de démarrer sans aucune sélection.
        charger_info = (coordinator.data or {}).get(charger_id) or {}
        default_badge = charger_info.get("current_card")
        if default_badge and charger_id not in selected_start_badge:
            selected_start_badge[charger_id] = default_badge

    def _badge_label(self, badge_id: str) -> str:
        name = self._badge_names.get(badge_id) or self.coordinator.badge_directory.get(badge_id)
        return f"{name} ({badge_id})" if name else badge_id

    def _known_badge_ids(self) -> set[str]:
        ids: set[str] = set()
        ids.update(self.coordinator.badge_directory.keys())
        ids.update(self._badge_names.keys())
        ids.update((self._badge_energy_coordinator.data or {}).keys())
        charger_info = (self.coordinator.data or {}).get(self._charger_id) or {}
        current_card = charger_info.get("current_card")
        if current_card:
            ids.add(current_card)
        last_transaction = charger_info.get("last_transaction") or {}
        if last_transaction.get("id_tag"):
            ids.add(last_transaction["id_tag"])
        return ids

    def _refresh_option_map(self) -> None:
        self._id_by_label = {NO_BADGE_OPTION: ""}
        for badge_id in sorted(self._known_badge_ids()):
            self._id_by_label[self._badge_label(badge_id)] = badge_id

    @property
    def options(self) -> list[str]:
        self._refresh_option_map()
        return list(self._id_by_label.keys())

    @property
    def current_option(self) -> str | None:
        if not self._id_by_label:
            self._refresh_option_map()
        selected_id = self._selected_start_badge.get(self._charger_id, "")
        for label, badge_id in self._id_by_label.items():
            if badge_id == selected_id:
                return label
        return NO_BADGE_OPTION

    async def async_select_option(self, option: str) -> None:
        badge_id = self._id_by_label.get(option, "")
        self._selected_start_badge[self._charger_id] = badge_id
        self.async_write_ha_state()
