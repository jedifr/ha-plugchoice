"""Capteurs Plugchoice: courants L1/L2/L3, tensions, puissance, énergie.

Une entité est créée pour chaque borne découverte par le
PlugchoiceChargersCoordinator, et de nouvelles entités sont ajoutées
automatiquement si de nouvelles bornes apparaissent plus tard sur le compte.
Un capteur d'énergie cumulée est en outre créé par badge RFID détecté
(PlugchoiceBadgeEnergyCoordinator), pour le suivi par utilisateur dans le
tableau Énergie de Home Assistant.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import async_ensure_meter_coordinator
from .const import CONF_BADGE_NAMES, DOMAIN, SIGNAL_LOAD_BALANCING_UPDATE
from .coordinator import (
    PlugchoiceBadgeEnergyCoordinator,
    PlugchoiceChargersCoordinator,
    PlugchoiceMeterCoordinator,
    connector_error_code,
    connector_status,
)
from .load_balancer import PlugchoiceLoadBalancer


def _parse_iso(timestamp: str | None) -> datetime | None:
    """Parse un timestamp ISO 8601 renvoyé par Plugchoice (suffixe 'Z' inclus)."""
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True, kw_only=True)
class PlugchoiceSensorDescription(SensorEntityDescription):
    """Décrit un capteur dérivé d'une clé du coordinator de relevés."""

    data_key: str = ""


SENSOR_DESCRIPTIONS: tuple[PlugchoiceSensorDescription, ...] = (
    PlugchoiceSensorDescription(
        key="current_l1",
        data_key="current_l1",
        translation_key="current_l1",
        name="Courant L1",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    PlugchoiceSensorDescription(
        key="current_l2",
        data_key="current_l2",
        translation_key="current_l2",
        name="Courant L2",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    PlugchoiceSensorDescription(
        key="current_l3",
        data_key="current_l3",
        translation_key="current_l3",
        name="Courant L3",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    PlugchoiceSensorDescription(
        key="voltage_l1",
        data_key="voltage_l1",
        translation_key="voltage_l1",
        name="Tension L1",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    PlugchoiceSensorDescription(
        key="voltage_l2",
        data_key="voltage_l2",
        translation_key="voltage_l2",
        name="Tension L2",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    PlugchoiceSensorDescription(
        key="voltage_l3",
        data_key="voltage_l3",
        translation_key="voltage_l3",
        name="Tension L3",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    PlugchoiceSensorDescription(
        key="power",
        data_key="power",
        translation_key="power",
        name="Puissance",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    PlugchoiceSensorDescription(
        key="energy",
        data_key="energy",
        translation_key="energy",
        name="Énergie totale",
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    PlugchoiceSensorDescription(
        key="current_offered",
        data_key="current_offered",
        translation_key="current_offered",
        name="Courant autorisé",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
)


@dataclass(frozen=True, kw_only=True)
class PlugchoiceChargerInfoSensorDescription(SensorEntityDescription):
    """Décrit un capteur qui lit directement un champ de l'objet borne."""

    value_fn: Callable[[dict[str, Any]], Any]


CHARGER_INFO_SENSOR_DESCRIPTIONS: tuple[PlugchoiceChargerInfoSensorDescription, ...] = (
    PlugchoiceChargerInfoSensorDescription(
        key="charge_point_id",
        translation_key="charge_point_id",
        name="ID point de charge",
        icon="mdi:identifier",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda info: info.get("identity"),
    ),
    PlugchoiceChargerInfoSensorDescription(
        key="manufacturer",
        translation_key="manufacturer",
        name="Fabricant",
        icon="mdi:factory",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda info: (info.get("model") or {}).get("vendor"),
    ),
    PlugchoiceChargerInfoSensorDescription(
        key="model_name",
        translation_key="model_name",
        name="Modèle",
        icon="mdi:ev-station",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda info: (info.get("model") or {}).get("name"),
    ),
    PlugchoiceChargerInfoSensorDescription(
        key="firmware_version",
        translation_key="firmware_version",
        name="Firmware",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda info: info.get("firmware_version"),
    ),
    PlugchoiceChargerInfoSensorDescription(
        key="max_current",
        translation_key="max_current",
        name="Courant maximal",
        icon="mdi:current-ac",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda info: info.get("max_current"),
    ),
)


class PlugchoiceChargerInfoSensor(CoordinatorEntity[PlugchoiceChargersCoordinator], SensorEntity):
    """Capteur générique pour les informations statiques d'une borne.

    Toutes ces informations proviennent de l'objet borne déjà récupéré par
    le coordinator de découverte (GET /chargers) : aucun appel API
    supplémentaire n'est nécessaire.
    """

    entity_description: PlugchoiceChargerInfoSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PlugchoiceChargersCoordinator,
        description: PlugchoiceChargerInfoSensorDescription,
        charger_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._charger_id = charger_id
        self._attr_unique_id = f"{charger_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger_id)},
            name=device_name,
            manufacturer="Plugchoice",
            model="Borne de recharge",
            configuration_url=f"https://app.plugchoice.com/chargers/{charger_id}",
        )

    @property
    def native_value(self) -> Any:
        charger_info = (self.coordinator.data or {}).get(self._charger_id) or {}
        value = self.entity_description.value_fn(charger_info)
        if self.entity_description.key == "max_current" and value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        return value


class PlugchoiceLastChargeDateSensor(
    CoordinatorEntity[PlugchoiceChargersCoordinator], SensorEntity
):
    """Date/heure de la dernière recharge (fin de la dernière session connue).

    Si une session est en cours (pas de stopped_at), retombe sur la date
    de démarrage de cette session.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "last_charge_date"
    _attr_name = "Date de la dernière recharge"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self,
        coordinator: PlugchoiceChargersCoordinator,
        charger_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._charger_id = charger_id
        self._attr_unique_id = f"{charger_id}_last_charge_date"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger_id)},
            name=device_name,
            manufacturer="Plugchoice",
            model="Borne de recharge",
            configuration_url=f"https://app.plugchoice.com/chargers/{charger_id}",
        )

    @property
    def native_value(self) -> datetime | None:
        charger_info = (self.coordinator.data or {}).get(self._charger_id) or {}
        transaction = charger_info.get("last_transaction") or {}
        return _parse_iso(transaction.get("stopped_at") or transaction.get("started_at"))


class PlugchoiceConnectorStatusSensor(
    CoordinatorEntity[PlugchoiceChargersCoordinator], SensorEntity
):
    """Statut OCPP du connecteur (Available, Preparing, Charging, SuspendedEV…).

    Répond d'un coup d'œil à « pourquoi la charge ne démarre pas » :
    `SuspendedEV` = voiture pleine / ne demande rien, `Finishing` = session
    en clôture, `Available` = prêt pour un démarrage.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "connector_status"
    _attr_name = "Statut"
    _attr_icon = "mdi:ev-station"

    def __init__(
        self,
        coordinator: PlugchoiceChargersCoordinator,
        charger_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._charger_id = charger_id
        self._attr_unique_id = f"{charger_id}_connector_status"
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
    def native_value(self) -> str | None:
        return connector_status(self._charger_info())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        charger_info = self._charger_info()
        transaction = charger_info.get("last_transaction") or {}
        return {
            "error_code": connector_error_code(charger_info),
            "session_in_progress": bool(
                transaction.get("started_at") and not transaction.get("stopped_at")
            ),
        }


class PlugchoiceChargingProfileSensor(
    CoordinatorEntity[PlugchoiceChargersCoordinator], SensorEntity
):
    """Dernier profil de charge (SetChargingProfile) appliqué à la borne.

    Lu depuis les logs OCPP bruts (pas les transactions) : un profil de
    charge définit une limite envoyée à la borne (ex: 32A), potentiellement
    liée à une transaction précise via son transactionId.

    La limite peut être exprimée en A ou en W selon le profil
    (chargingRateUnit) : l'état de ce capteur est donc un texte
    "valeur unité" plutôt qu'un nombre avec une unité fixe. Les 9 champs
    bruts restent disponibles individuellement en attributs.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "charging_profile"
    _attr_name = "Profil de charge actif"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:tune-variant"

    def __init__(
        self,
        coordinator: PlugchoiceChargersCoordinator,
        charger_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._charger_id = charger_id
        self._attr_unique_id = f"{charger_id}_charging_profile"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger_id)},
            name=device_name,
            manufacturer="Plugchoice",
            model="Borne de recharge",
            configuration_url=f"https://app.plugchoice.com/chargers/{charger_id}",
        )

    def _profile(self) -> dict[str, Any]:
        charger_info = (self.coordinator.data or {}).get(self._charger_id) or {}
        return charger_info.get("charging_profile") or {}

    @property
    def native_value(self) -> str | None:
        profile = self._profile()
        limit = profile.get("limit")
        if limit is None:
            return None
        unit = profile.get("charging_rate_unit")
        return f"{limit} {unit}" if unit else str(limit)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        profile = self._profile()
        return {
            "charging_profile_id": profile.get("charging_profile_id"),
            "connector_id": profile.get("connector_id"),
            "limit": profile.get("limit"),
            "start_period": profile.get("start_period"),
            "number_phases": profile.get("number_phases"),
            "transaction_id": profile.get("transaction_id"),
            "stack_level": profile.get("stack_level"),
            "charging_profile_purpose": profile.get("charging_profile_purpose"),
            "charging_profile_kind": profile.get("charging_profile_kind"),
            "charging_rate_unit": profile.get("charging_rate_unit"),
            "status": profile.get("status"),
            "applied_at": profile.get("applied_at"),
        }


class _PlugchoiceLoadBalancingSensor(SensorEntity):
    """Base commune aux capteurs diagnostic du load balancing.

    Pas de DataUpdateCoordinator ici : la valeur vient directement de
    l'objet PlugchoiceLoadBalancer, mis à jour via un signal dispatcher à
    chaque réévaluation (cf. load_balancer.py).
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, load_balancer: PlugchoiceLoadBalancer, entry_id: str, unique_suffix: str) -> None:
        self._load_balancer = load_balancer
        self._attr_unique_id = f"{entry_id}_{unique_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_load_balancing")},
            name="Répartition de puissance",
            manufacturer="Plugchoice",
            model="Load balancing",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._load_balancer.signal, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class PlugchoiceSmoothedPowerSensor(_PlugchoiceLoadBalancingSensor):
    """Puissance réseau lissée sur la fenêtre configurée (positif = tirage)."""

    _attr_translation_key = "load_balancing_smoothed_power"
    _attr_name = "Puissance réseau lissée"
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, load_balancer: PlugchoiceLoadBalancer, entry_id: str) -> None:
        super().__init__(load_balancer, entry_id, "load_balancing_smoothed_power")

    @property
    def native_value(self) -> float | None:
        value = self._load_balancer.smoothed_power
        return round(value) if value is not None else None


class PlugchoiceAvailableBudgetSensor(_PlugchoiceLoadBalancingSensor):
    """Budget de puissance restant pour la recharge (peut être négatif si dépassé)."""

    _attr_translation_key = "load_balancing_available_budget"
    _attr_name = "Budget EV disponible"
    _attr_icon = "mdi:ev-station"

    def __init__(self, load_balancer: PlugchoiceLoadBalancer, entry_id: str) -> None:
        super().__init__(load_balancer, entry_id, "load_balancing_available_budget")

    @property
    def native_value(self) -> float | None:
        value = self._load_balancer.available_ev_budget
        return round(value) if value is not None else None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Crée les entités pour toutes les bornes déjà découvertes, et s'abonne aux nouvelles."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    chargers_coordinator: PlugchoiceChargersCoordinator = domain_data["chargers_coordinator"]
    badge_energy_coordinator: PlugchoiceBadgeEnergyCoordinator = domain_data[
        "badge_energy_coordinator"
    ]
    # Lu une fois au setup : un changement (via les options) déclenche un
    # reload complet de l'entrée, donc les entités seront recréées avec la
    # version à jour de ce mapping.
    badge_names: dict[str, str] = entry.options.get(CONF_BADGE_NAMES, {})

    load_balancer: PlugchoiceLoadBalancer | None = domain_data.get("load_balancer")
    if load_balancer is not None:
        async_add_entities(
            [
                PlugchoiceSmoothedPowerSensor(load_balancer, entry.entry_id),
                PlugchoiceAvailableBudgetSensor(load_balancer, entry.entry_id),
            ]
        )

    known_charger_ids: set[str] = set()
    known_badge_ids: set[str] = set()

    async def _add_entities_for_charger(charger_id: str, charger_name: str) -> None:
        meter_coordinator = await async_ensure_meter_coordinator(hass, entry, charger_id)
        entities = [
            PlugchoiceSensor(meter_coordinator, description, charger_id, charger_name)
            for description in SENSOR_DESCRIPTIONS
        ]
        entities.append(
            PlugchoiceReferenceSensor(chargers_coordinator, charger_id, charger_name)
        )
        entities.append(
            PlugchoicePlugChargeCardSensor(
                chargers_coordinator, charger_id, charger_name, badge_names
            )
        )
        entities.append(
            PlugchoiceLastSessionBadgeSensor(
                chargers_coordinator, charger_id, charger_name, badge_names
            )
        )
        entities.append(
            PlugchoiceSessionEnergySensor(chargers_coordinator, charger_id, charger_name)
        )
        entities.append(
            PlugchoiceSessionDurationSensor(chargers_coordinator, charger_id, charger_name)
        )
        entities.append(
            PlugchoiceLastCompletedSessionEnergySensor(
                chargers_coordinator, charger_id, charger_name
            )
        )
        entities.append(
            PlugchoiceLastCompletedSessionDurationSensor(
                chargers_coordinator, charger_id, charger_name
            )
        )
        entities.extend(
            PlugchoiceChargerInfoSensor(chargers_coordinator, description, charger_id, charger_name)
            for description in CHARGER_INFO_SENSOR_DESCRIPTIONS
        )
        entities.append(
            PlugchoiceLastChargeDateSensor(chargers_coordinator, charger_id, charger_name)
        )
        entities.append(
            PlugchoiceChargingProfileSensor(chargers_coordinator, charger_id, charger_name)
        )
        entities.append(
            PlugchoiceConnectorStatusSensor(chargers_coordinator, charger_id, charger_name)
        )
        async_add_entities(entities)
        known_charger_ids.add(charger_id)

    def _badge_display_name(badge_id: str) -> str:
        return (
            badge_names.get(badge_id) or chargers_coordinator.badge_directory.get(badge_id) or badge_id
        )

    def _add_entities_for_badge(badge_id: str) -> None:
        async_add_entities(
            [
                PlugchoiceBadgeEnergySensor(
                    badge_energy_coordinator, badge_id, _badge_display_name(badge_id)
                )
            ]
        )
        known_badge_ids.add(badge_id)

    # Bornes déjà connues au moment du setup.
    for charger_id, charger_info in chargers_coordinator.data.items():
        name = _charger_display_name(charger_id, charger_info)
        await _add_entities_for_charger(charger_id, name)

    # Badges déjà connus (via l'agrégation d'énergie) au moment du setup.
    for badge_id in badge_energy_coordinator.data:
        _add_entities_for_badge(badge_id)

    # Découverte continue : à chaque rafraîchissement de la liste des bornes
    # (toutes les 10 min par défaut), on ajoute les entités des bornes
    # nouvellement apparues sans jamais retirer les existantes.
    async def _handle_chargers_update() -> None:
        for charger_id, charger_info in chargers_coordinator.data.items():
            if charger_id in known_charger_ids:
                continue
            name = _charger_display_name(charger_id, charger_info)
            await _add_entities_for_charger(charger_id, name)

    def _schedule_handle_chargers_update() -> None:
        # async_add_listener attend un callback synchrone : on planifie la
        # coroutine sur la boucle d'événements plutôt que de l'attendre ici.
        hass.async_create_task(_handle_chargers_update())

    entry.async_on_unload(
        chargers_coordinator.async_add_listener(_schedule_handle_chargers_update)
    )

    # Découverte continue des badges : chaque nouveau badge apparu dans une
    # transaction obtient son propre capteur d'énergie cumulée.
    def _handle_badge_energy_update() -> None:
        for badge_id in badge_energy_coordinator.data:
            if badge_id in known_badge_ids:
                continue
            _add_entities_for_badge(badge_id)

    entry.async_on_unload(
        badge_energy_coordinator.async_add_listener(_handle_badge_energy_update)
    )


def _charger_display_name(charger_id: str, charger_info: dict) -> str:
    """Choisit le meilleur nom d'affichage disponible pour une borne.

    L'API n'expose pas de champ "name" : on utilise en priorité la
    référence personnalisée ("reference", souvent définie côté portail),
    puis l'identité OCPP ("identity"), puis un repli sur l'UUID tronqué.
    """
    reference = charger_info.get("reference")
    if reference:
        return reference
    identity = charger_info.get("identity")
    if identity:
        return identity
    return f"Borne {charger_id[:8]}"


def _resolve_badge_name(
    badge_id: str | None, manual_names: dict[str, str], auto_directory: dict[str, str]
) -> str | None:
    """Résout le nom d'un badge : override manuel > annuaire Plugchoice > None.

    L'annuaire Plugchoice vient des cartes RFID déjà nommées sur la
    plateforme (Location > Cards). Le mapping manuel (options de
    l'intégration) permet de surcharger un nom pour l'affichage HA
    uniquement, sans toucher à la configuration Plugchoice.
    """
    if not badge_id:
        return None
    return manual_names.get(badge_id) or auto_directory.get(badge_id)


class PlugchoiceSensor(CoordinatorEntity[PlugchoiceMeterCoordinator], SensorEntity):
    """Un capteur Plugchoice, ex: le courant L1 d'une borne donnée."""

    entity_description: PlugchoiceSensorDescription
    _attr_has_entity_name = True

    # Mesures instantanées : l'absence de relevé (borne au repos, pas de
    # session en cours) signifie physiquement "rien ne circule", donc 0 est
    # une valeur plus juste que "inconnu". On exclut volontairement les
    # tensions (la ligne reste sous tension même sans session : 0V serait
    # faux) et l'énergie cumulée (un compteur ne redescend jamais à zéro :
    # afficher 0 y créerait une fausse remise à zéro dans l'historique
    # "total_increasing" du tableau Énergie de HA).
    _ZERO_WHEN_IDLE_KEYS = {"current_l1", "current_l2", "current_l3", "power", "current_offered"}

    def __init__(
        self,
        coordinator: PlugchoiceMeterCoordinator,
        description: PlugchoiceSensorDescription,
        charger_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{charger_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger_id)},
            name=device_name,
            manufacturer="Plugchoice",
            model="Borne de recharge",
            configuration_url=f"https://app.plugchoice.com/chargers/{charger_id}",
        )

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        data_key = self.entity_description.data_key
        value = self.coordinator.data.get(data_key)
        if value is None and data_key in self._ZERO_WHEN_IDLE_KEYS:
            return 0.0
        return value


class PlugchoiceReferenceSensor(CoordinatorEntity[PlugchoiceChargersCoordinator], SensorEntity):
    """Expose la référence personnalisée de la borne comme capteur texte.

    Contrairement aux autres capteurs, celui-ci lit le coordinator de
    découverte (peu fréquent) plutôt que le coordinator de relevés
    (fréquent), puisque la référence ne vient pas des meter values.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "reference"
    _attr_name = "Référence"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:tag-text-outline"

    def __init__(
        self,
        coordinator: PlugchoiceChargersCoordinator,
        charger_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._charger_id = charger_id
        self._attr_unique_id = f"{charger_id}_reference"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger_id)},
            name=device_name,
            manufacturer="Plugchoice",
            model="Borne de recharge",
            configuration_url=f"https://app.plugchoice.com/chargers/{charger_id}",
        )

    @property
    def native_value(self) -> str | None:
        charger_info = (self.coordinator.data or {}).get(self._charger_id) or {}
        return charger_info.get("reference")


class PlugchoicePlugChargeCardSensor(CoordinatorEntity[PlugchoiceChargersCoordinator], SensorEntity):
    """Expose le badge RFID configuré pour le Plug & Charge de la borne.

    Note : ceci reflète la carte configurée pour l'auto-démarrage, pas
    la carte utilisée lors de la dernière session de charge (voir
    PlugchoiceLastSessionBadgeSensor pour ça).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "plug_charge_card"
    _attr_name = "Badge Plug & Charge"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:credit-card-wireless-outline"

    def __init__(
        self,
        coordinator: PlugchoiceChargersCoordinator,
        charger_id: str,
        device_name: str,
        badge_names: dict[str, str],
    ) -> None:
        super().__init__(coordinator)
        self._charger_id = charger_id
        self._badge_names = badge_names
        self._attr_unique_id = f"{charger_id}_active_card"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger_id)},
            name=device_name,
            manufacturer="Plugchoice",
            model="Borne de recharge",
            configuration_url=f"https://app.plugchoice.com/chargers/{charger_id}",
        )

    @property
    def native_value(self) -> str | None:
        charger_info = (self.coordinator.data or {}).get(self._charger_id) or {}
        badge_id = charger_info.get("current_card")
        resolved = _resolve_badge_name(badge_id, self._badge_names, self.coordinator.badge_directory)
        return resolved or badge_id

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        charger_info = (self.coordinator.data or {}).get(self._charger_id) or {}
        badge_id = charger_info.get("current_card")
        return {
            "badge_id": badge_id,
            "badge_name": _resolve_badge_name(
                badge_id, self._badge_names, self.coordinator.badge_directory
            ),
            "plug_charge_enabled": charger_info.get("plug_charge_enabled"),
        }


class PlugchoiceSessionEnergySensor(CoordinatorEntity[PlugchoiceChargersCoordinator], SensorEntity):
    """Énergie consommée par la session de charge actuellement en cours.

    Retombe à 0 dès qu'aucune session n'est active (pas de valeur "figée"
    sur la dernière session terminée). Contrairement au capteur d'énergie
    cumulée par badge, celui-ci repart de zéro à chaque nouvelle session :
    ce n'est PAS une valeur croissante, donc à ne pas ajouter au tableau
    Énergie de HA (utiliser plutôt les capteurs "Énergie [badge]" pour ça).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "current_session_energy"
    _attr_name = "Énergie (session en cours)"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:lightning-bolt"

    def __init__(
        self,
        coordinator: PlugchoiceChargersCoordinator,
        charger_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._charger_id = charger_id
        self._attr_unique_id = f"{charger_id}_session_energy"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger_id)},
            name=device_name,
            manufacturer="Plugchoice",
            model="Borne de recharge",
            configuration_url=f"https://app.plugchoice.com/chargers/{charger_id}",
        )

    @property
    def native_value(self) -> float:
        charger_info = (self.coordinator.data or {}).get(self._charger_id) or {}
        transaction = charger_info.get("last_transaction") or {}
        session_in_progress = (
            transaction.get("started_at") is not None and transaction.get("stopped_at") is None
        )
        # Hors session active, on affiche 0 plutôt que la valeur de la
        # dernière session terminée : ce capteur reflète "la charge en
        # cours", pas un historique.
        if not session_in_progress:
            return 0.0
        kwh = transaction.get("total_kwh")
        try:
            return float(kwh) if kwh is not None else 0.0
        except (TypeError, ValueError):
            return 0.0


class PlugchoiceSessionDurationSensor(
    CoordinatorEntity[PlugchoiceChargersCoordinator], SensorEntity
):
    """Durée de la session de charge actuellement en cours.

    Retombe à 0 dès qu'aucune session n'est active (pas de valeur "figée"
    sur la dernière session terminée). Recalculée par rapport à
    "maintenant" à chaque rafraîchissement du coordinator (toutes les
    10 min par défaut) : ce n'est donc pas un chronomètre en temps réel,
    mais une valeur mise à jour périodiquement.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "current_session_duration"
    _attr_name = "Durée (session en cours)"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer-outline"

    def __init__(
        self,
        coordinator: PlugchoiceChargersCoordinator,
        charger_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._charger_id = charger_id
        self._attr_unique_id = f"{charger_id}_session_duration"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger_id)},
            name=device_name,
            manufacturer="Plugchoice",
            model="Borne de recharge",
            configuration_url=f"https://app.plugchoice.com/chargers/{charger_id}",
        )

    @property
    def native_value(self) -> float:
        charger_info = (self.coordinator.data or {}).get(self._charger_id) or {}
        transaction = charger_info.get("last_transaction") or {}
        started_at = _parse_iso(transaction.get("started_at"))
        stopped_at_raw = transaction.get("stopped_at")
        # Hors session active (jamais démarrée, ou déjà terminée), on
        # affiche 0 plutôt que la durée de la dernière session terminée :
        # ce capteur reflète "la charge en cours", pas un historique.
        if started_at is None or stopped_at_raw is not None:
            return 0.0
        return round((datetime.now(timezone.utc) - started_at).total_seconds() / 60, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        charger_info = (self.coordinator.data or {}).get(self._charger_id) or {}
        transaction = charger_info.get("last_transaction") or {}
        return {
            "session_in_progress": transaction.get("started_at") is not None
            and transaction.get("stopped_at") is None,
        }


class PlugchoiceLastCompletedSessionEnergySensor(
    CoordinatorEntity[PlugchoiceChargersCoordinator], SensorEntity
):
    """Énergie consommée par la dernière session terminée (jamais en cours).

    Contrairement au capteur "Énergie (session en cours)", celui-ci ne
    retombe pas à 0 entre deux sessions : il garde la valeur de la
    dernière charge complète jusqu'à ce qu'une nouvelle session se termine.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "last_completed_session_energy"
    _attr_name = "Énergie (dernière session)"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:lightning-bolt-outline"

    def __init__(
        self,
        coordinator: PlugchoiceChargersCoordinator,
        charger_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._charger_id = charger_id
        self._attr_unique_id = f"{charger_id}_last_completed_session_energy"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger_id)},
            name=device_name,
            manufacturer="Plugchoice",
            model="Borne de recharge",
            configuration_url=f"https://app.plugchoice.com/chargers/{charger_id}",
        )

    def _last_completed(self) -> dict[str, Any]:
        charger_info = (self.coordinator.data or {}).get(self._charger_id) or {}
        return charger_info.get("last_completed_transaction") or {}

    @property
    def native_value(self) -> float:
        kwh = self._last_completed().get("total_kwh")
        try:
            return float(kwh) if kwh is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        transaction = self._last_completed()
        return {
            "started_at": transaction.get("started_at"),
            "stopped_at": transaction.get("stopped_at"),
            "badge_id": transaction.get("id_tag"),
        }


class PlugchoiceLastCompletedSessionDurationSensor(
    CoordinatorEntity[PlugchoiceChargersCoordinator], SensorEntity
):
    """Durée de la dernière session terminée (jamais en cours).

    Contrairement au capteur "Durée (session en cours)", celui-ci ne
    retombe pas à 0 entre deux sessions : il garde la valeur de la
    dernière charge complète jusqu'à ce qu'une nouvelle session se termine.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "last_completed_session_duration"
    _attr_name = "Durée (dernière session)"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer-outline"

    def __init__(
        self,
        coordinator: PlugchoiceChargersCoordinator,
        charger_id: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._charger_id = charger_id
        self._attr_unique_id = f"{charger_id}_last_completed_session_duration"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger_id)},
            name=device_name,
            manufacturer="Plugchoice",
            model="Borne de recharge",
            configuration_url=f"https://app.plugchoice.com/chargers/{charger_id}",
        )

    def _last_completed(self) -> dict[str, Any]:
        charger_info = (self.coordinator.data or {}).get(self._charger_id) or {}
        return charger_info.get("last_completed_transaction") or {}

    @property
    def native_value(self) -> float:
        transaction = self._last_completed()
        started_at = _parse_iso(transaction.get("started_at"))
        stopped_at = _parse_iso(transaction.get("stopped_at"))
        if started_at is None or stopped_at is None:
            return 0.0
        return round((stopped_at - started_at).total_seconds() / 60, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        transaction = self._last_completed()
        return {
            "started_at": transaction.get("started_at"),
            "stopped_at": transaction.get("stopped_at"),
            "badge_id": transaction.get("id_tag"),
        }


class PlugchoiceBadgeEnergySensor(CoordinatorEntity[PlugchoiceBadgeEnergyCoordinator], SensorEntity):
    """Énergie cumulée consommée par un badge, toutes bornes/sessions confondues.

    Chaque badge devient son propre "appareil" virtuel dans HA (indépendant
    des bornes), ce qui permet de l'ajouter comme "appareil individuel"
    dans le tableau Énergie et de suivre la consommation par utilisateur.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "badge_energy"
    _attr_name = "Énergie"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    # Cumul strictement croissant (somme de toutes les transactions connues) :
    # c'est la condition requise pour être utilisable dans le tableau Énergie.
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:account-card-outline"

    def __init__(
        self,
        coordinator: PlugchoiceBadgeEnergyCoordinator,
        badge_id: str,
        badge_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._badge_id = badge_id
        self._attr_unique_id = f"badge_{badge_id}_energy"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"badge_{badge_id}")},
            name=badge_name,
            manufacturer="Plugchoice",
            model="Badge RFID",
        )

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self._badge_id)
        return round(value, 3) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"badge_id": self._badge_id}


class PlugchoiceLastSessionBadgeSensor(
    CoordinatorEntity[PlugchoiceChargersCoordinator], SensorEntity
):
    """Expose le badge RFID (id_tag) qui a démarré la dernière session de charge.

    Contrairement au badge Plug & Charge (configuration), celui-ci reflète
    ce qui s'est réellement passé : l'id_tag de la transaction la plus
    récente, qu'elle soit en cours ou terminée.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "last_session_badge"
    _attr_name = "Dernier badge (session)"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:card-account-details-outline"

    def __init__(
        self,
        coordinator: PlugchoiceChargersCoordinator,
        charger_id: str,
        device_name: str,
        badge_names: dict[str, str],
    ) -> None:
        super().__init__(coordinator)
        self._charger_id = charger_id
        self._badge_names = badge_names
        self._attr_unique_id = f"{charger_id}_last_session_badge"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, charger_id)},
            name=device_name,
            manufacturer="Plugchoice",
            model="Borne de recharge",
            configuration_url=f"https://app.plugchoice.com/chargers/{charger_id}",
        )

    def _last_transaction(self) -> dict[str, Any]:
        charger_info = (self.coordinator.data or {}).get(self._charger_id) or {}
        return charger_info.get("last_transaction") or {}

    @property
    def native_value(self) -> str | None:
        badge_id = self._last_transaction().get("id_tag")
        resolved = _resolve_badge_name(badge_id, self._badge_names, self.coordinator.badge_directory)
        return resolved or badge_id

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        transaction = self._last_transaction()
        badge_id = transaction.get("id_tag")
        return {
            "badge_id": badge_id,
            "badge_name": _resolve_badge_name(
                badge_id, self._badge_names, self.coordinator.badge_directory
            ),
            "started_at": transaction.get("started_at"),
            "stopped_at": transaction.get("stopped_at"),
            "total_kwh": transaction.get("total_kwh"),
            # Une session en cours n'a pas encore de stopped_at.
            "session_in_progress": transaction.get("started_at") is not None
            and transaction.get("stopped_at") is None,
        }
