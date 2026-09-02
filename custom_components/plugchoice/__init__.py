"""Intégration Home Assistant pour Plugchoice (bornes de recharge VE).

Un token de compte = une entrée de config. Les bornes accessibles avec ce
token sont découvertes automatiquement (et périodiquement) : pas besoin de
connaître ou saisir leurs UUID.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PlugchoiceClient
from .const import (
    CONF_LOAD_BALANCING_ENABLED,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
)
from .coordinator import (
    PlugchoiceBadgeEnergyCoordinator,
    PlugchoiceChargersCoordinator,
    PlugchoiceMeterCoordinator,
)
from .load_balancer import PlugchoiceLoadBalancer

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
    }

    if entry.options.get(CONF_LOAD_BALANCING_ENABLED):
        load_balancer = PlugchoiceLoadBalancer(
            hass,
            entry,
            client,
            chargers_coordinator,
            lambda charger_id: async_ensure_meter_coordinator(hass, entry, charger_id),
            hass.data[DOMAIN][entry.entry_id]["boosted_chargers"],
        )
        load_balancer.async_start()
        entry.async_on_unload(load_balancer.async_stop)
        hass.data[DOMAIN][entry.entry_id]["load_balancer"] = load_balancer

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Décharge proprement l'entrée de config."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Recharge l'entrée quand ses options changent (ex: nouveau token, intervalle)."""
    await hass.config_entries.async_reload(entry.entry_id)


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
