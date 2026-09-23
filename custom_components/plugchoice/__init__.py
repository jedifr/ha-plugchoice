"""Intégration Home Assistant pour Plugchoice (bornes de recharge VE).

Un token de compte = une entrée de config. Les bornes accessibles avec ce
token sont découvertes automatiquement (et périodiquement) : pas besoin de
connaître ou saisir leurs UUID.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntry

from .api import PlugchoiceApiError, PlugchoiceClient
from .const import (
    CONF_LOAD_BALANCING_ENABLED,
    CONF_SCAN_INTERVAL,
    DEFAULT_CONNECTOR_ID,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    LOAD_BALANCING_TEMPORARILY_DISABLED,
)
from .coordinator import (
    PlugchoiceBadgeEnergyCoordinator,
    PlugchoiceChargersCoordinator,
    PlugchoiceMeterCoordinator,
)
from .load_balancer import PlugchoiceLoadBalancer

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.LOCK,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Initialise l'intégration: découvre les bornes puis prépare un coordinator par borne."""
    session = async_get_clientsession(hass)
    client = PlugchoiceClient(session, entry.data[CONF_TOKEN])

    chargers_coordinator = PlugchoiceChargersCoordinator(hass, client)
    await chargers_coordinator.async_config_entry_first_refresh()

    badge_energy_coordinator = PlugchoiceBadgeEnergyCoordinator(hass, chargers_coordinator)
    await badge_energy_coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "chargers_coordinator": chargers_coordinator,
        "badge_energy_coordinator": badge_energy_coordinator,
        # Rempli au fur et à mesure par ensure_meter_coordinator() : un
        # PlugchoiceMeterCoordinator par borne découverte.
        "meter_coordinators": {},
        "load_balancer": None,
        # Rempli par select.py : {charger_id: badge_id} — le badge choisi
        # pour le prochain démarrage à distance sur chaque borne.
        "selected_start_badge": {},
        # Rempli par switch.py : ensemble des charger_id actuellement en
        # mode "Boost" (exemptés du partage de budget par le load balancer).
        "boosted_chargers": set(),
        # Rempli par number.py : {charger_id: courant demandé en A} pour un
        # réglage manuel du slider "Limite de charge" en cours de session —
        # exempte la borne du partage de budget jusqu'à la fin de la
        # session (cf. load_balancer.py), sans quoi le régulateur écrase le
        # réglage manuel dès son cycle suivant.
        "manual_current_overrides": {},
    }

    if LOAD_BALANCING_TEMPORARILY_DISABLED:
        if entry.options.get(CONF_LOAD_BALANCING_ENABLED):
            _LOGGER.warning(
                "Répartition de puissance : option activée dans la config, mais "
                "temporairement désactivée en dur le temps d'une investigation "
                "(LOAD_BALANCING_TEMPORARILY_DISABLED dans const.py) — aucun "
                "cycle du régulateur ne tournera. Le slider, le Boost et les "
                "boutons restent fonctionnels normalement."
            )
    elif entry.options.get(CONF_LOAD_BALANCING_ENABLED):
        load_balancer = PlugchoiceLoadBalancer(
            hass,
            entry,
            client,
            chargers_coordinator,
            lambda charger_id: async_ensure_meter_coordinator(hass, entry, charger_id),
            hass.data[DOMAIN][entry.entry_id]["boosted_chargers"],
            hass.data[DOMAIN][entry.entry_id]["manual_current_overrides"],
        )
        load_balancer.async_start()
        entry.async_on_unload(load_balancer.async_stop)
        hass.data[DOMAIN][entry.entry_id]["load_balancer"] = load_balancer

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Nettoyage des appareils "orphelins" : une borne renommée/remplacée
    # côté Plugchoice (nouvel UUID) laisserait sinon l'ancien appareil
    # grisé indéfiniment dans HA, rien ne le retirant jamais de lui-même.
    await _async_prune_stale_charger_devices(hass, entry, chargers_coordinator)

    # Idem pour l'appareil virtuel "Répartition de puissance" : tant que
    # LOAD_BALANCING_TEMPORARILY_DISABLED est actif, aucune entité n'est
    # recréée dessus (cf. sensor.py), mais l'appareil créé par une version
    # antérieure (régulateur alors actif) restait affiché, grisé, sans
    # recours — on le retire explicitement.
    await _async_prune_disabled_load_balancing_device(hass, entry)

    @callback
    def _schedule_prune_stale_devices() -> None:
        hass.async_create_task(
            _async_prune_stale_charger_devices(hass, entry, chargers_coordinator)
        )

    entry.async_on_unload(
        chargers_coordinator.async_add_listener(_schedule_prune_stale_devices)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Autorise le retrait manuel de n'importe quel appareil de l'intégration.

    Sans ceci, Home Assistant masque le bouton "Supprimer l'appareil" tant
    que l'intégration ne confirme pas explicitement que c'est sûr — un
    appareil orphelin (borne remplacée/supprimée côté Plugchoice) resterait
    alors bloqué, grisé, sans recours manuel (le nettoyage automatique de
    `_async_prune_stale_charger_devices` couvre déjà le cas normal, ceci
    n'est qu'un filet de sécurité).
    """
    return True


async def _async_prune_stale_charger_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    chargers_coordinator: PlugchoiceChargersCoordinator,
) -> None:
    """Retire les appareils de borne dont l'UUID n'existe plus côté Plugchoice.

    L'intégration ajoute un appareil par borne découverte mais n'en a
    jamais retiré : une borne renommée avec un nouvel UUID (remplacement
    matériel) ou supprimée côté Plugchoice laissait l'ancien appareil grisé
    dans HA indéfiniment. Ne touche ni aux appareils "badge" (identifiant
    `badge_<id>`) ni à celui du load balancing (identifiant
    `<entry_id>_load_balancing`), qui ne sont pas des bornes.
    """
    known_charger_ids = set(chargers_coordinator.data or {})
    load_balancing_identifier = f"{entry.entry_id}_load_balancing"
    device_registry = dr.async_get(hass)

    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        for identifier_domain, identifier_value in device.identifiers:
            if identifier_domain != DOMAIN:
                continue
            if identifier_value.startswith("badge_") or identifier_value == load_balancing_identifier:
                break
            if identifier_value not in known_charger_ids:
                _LOGGER.info(
                    "Borne %s introuvable côté Plugchoice : retrait de l'appareil "
                    "orphelin correspondant dans Home Assistant",
                    identifier_value,
                )
                device_registry.async_remove_device(device.id)
            break


async def _async_prune_disabled_load_balancing_device(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Retire l'appareil "Répartition de puissance" tant que la fonction est coupée.

    `sensor.py` ne recrée ses 2 entités que si `load_balancer` a été
    instancié (donc jamais tant que `LOAD_BALANCING_TEMPORARILY_DISABLED`
    est actif) : sans ce nettoyage, un appareil créé par une version
    antérieure (régulateur alors actif) restait affiché indéfiniment,
    grisé, sans plus aucune donnée.
    """
    if not LOAD_BALANCING_TEMPORARILY_DISABLED:
        return

    identifier = f"{entry.entry_id}_load_balancing"
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, identifier)})
    if device is not None:
        _LOGGER.info(
            "Répartition de puissance désactivée : retrait de son appareil "
            "diagnostic dans Home Assistant"
        )
        device_registry.async_remove_device(device.id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Décharge proprement l'entrée de config."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Recharge l'entrée quand ses options changent (ex: nouveau token, intervalle)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Nettoyage à la suppression de l'intégration.

    Retire de chaque borne le profil de limite de charge posé par
    l'intégration (stackLevel dédié), pour ne pas laisser une borne bridée
    par une limite « fantôme » après désinstallation. Best-effort : appelé
    uniquement à la suppression (pas au simple rechargement/redémarrage),
    et toute erreur est seulement journalisée.

    ⚠️ Utilise l'endpoint `actions/clear-charge-limit`, non confirmé par la
    documentation Plugchoice.
    """
    session = async_get_clientsession(hass)
    client = PlugchoiceClient(session, entry.data[CONF_TOKEN])
    try:
        chargers = await client.async_list_chargers()
    except PlugchoiceApiError as err:
        _LOGGER.warning(
            "Nettoyage à la suppression : impossible de lister les bornes (%s). "
            "Une limite de charge posée par l'intégration peut subsister sur "
            "certaines bornes ; la retirer via le portail Plugchoice si besoin.",
            err,
        )
        return

    for charger in chargers:
        charger_id = charger.get("uuid") or charger.get("id")
        if charger_id is None:
            continue
        try:
            await client.async_clear_charging_limit(str(charger_id), DEFAULT_CONNECTOR_ID)
        except PlugchoiceApiError as err:
            _LOGGER.warning(
                "Nettoyage à la suppression : échec du retrait de la limite sur %s (%s)",
                charger_id,
                err,
            )


async def async_ensure_meter_coordinator(
    hass: HomeAssistant, entry: ConfigEntry, charger_id: str
) -> PlugchoiceMeterCoordinator:
    """Retourne le coordinator de relevés pour cette borne, en le créant si besoin.

    Appelé depuis sensor.py, aussi bien au setup initial que lorsqu'une
    nouvelle borne apparaît dans la liste découverte par chargers_coordinator.
    """
    domain_data = hass.data[DOMAIN][entry.entry_id]
    meter_coordinators: dict[str, PlugchoiceMeterCoordinator] = domain_data["meter_coordinators"]

    if charger_id in meter_coordinators:
        return meter_coordinators[charger_id]

    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS)
    coordinator = PlugchoiceMeterCoordinator(
        hass, domain_data["client"], charger_id, scan_interval
    )
    await coordinator.async_config_entry_first_refresh()
    meter_coordinators[charger_id] = coordinator
    return coordinator
