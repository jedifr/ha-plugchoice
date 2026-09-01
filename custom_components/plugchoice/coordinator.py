"""Coordinators Plugchoice.

Deux niveaux :
- PlugchoiceChargersCoordinator : liste périodiquement les bornes du compte
  (découverte automatique, y compris de nouvelles bornes ajoutées plus tard).
- PlugchoiceMeterCoordinator : un par borne, interroge ses relevés de compteur.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PlugchoiceApiError, PlugchoiceAuthError, PlugchoiceClient
from .const import BADGE_ENERGY_INTERVAL, DISCOVERY_INTERVAL, DOMAIN, SENSOR_MAP

_LOGGER = logging.getLogger(__name__)


class PlugchoiceChargersCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Maintient la liste à jour des bornes accessibles avec ce token.

    Expose un dict {charger_id: infos_borne}. Les entités s'abonnent à ce
    coordinator pour détecter l'apparition de nouvelles bornes.
    """

    def __init__(self, hass: HomeAssistant, client: PlugchoiceClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_chargers",
            update_interval=DISCOVERY_INTERVAL,
        )
        self._client = client
        # Annuaire {id_token: name} des badges RFID enregistrés sur
        # Plugchoice (via les sites), reconstruit à chaque cycle de
        # découverte. Distinct de "result" (qui est par borne) : un badge
        # n'est pas rattaché à une borne précise.
        self.badge_directory: dict[str, str] = {}

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            chargers = await self._client.async_list_chargers()
        except PlugchoiceAuthError as err:
            raise UpdateFailed(f"Authentification Plugchoice refusée: {err}") from err
        except PlugchoiceApiError as err:
            raise UpdateFailed(f"Erreur API Plugchoice: {err}") from err

        result: dict[str, dict[str, Any]] = {}
        for charger in chargers:
            # Les objets Plugchoice exposent un "uuid" (utilisé dans les routes,
            # ex: /chargers/{uuid}/metervalues) distinct de leur "id" numérique
            # interne. On préfère "uuid" et on retombe sur "id" par sécurité.
            charger_id = charger.get("uuid") or charger.get("id")
            if charger_id is None:
                continue
            result[str(charger_id)] = charger

        # Complète chaque borne avec son badge RFID actuellement configuré
        # pour le Plug & Charge. Appel séparé par borne : on isole les
        # erreurs individuelles pour ne pas faire échouer toute la
        # découverte si une seule borne répond mal (ex: fonctionnalité
        # non supportée sur ce modèle).
        for charger_id, charger_info in result.items():
            try:
                plug_charge = await self._client.async_get_plug_charge_status(charger_id)
            except PlugchoiceApiError as err:
                _LOGGER.debug(
                    "Impossible de récupérer le statut Plug & Charge de %s: %s",
                    charger_id,
                    err,
                )
                continue
            charger_info["current_card"] = plug_charge.get("current_card")
            charger_info["plug_charge_enabled"] = plug_charge.get("enabled")

        # Complète chaque borne avec son statut de verrouillage.
        for charger_id, charger_info in result.items():
            try:
                lock_status = await self._client.async_get_lock_status(charger_id)
            except PlugchoiceApiError as err:
                _LOGGER.debug(
                    "Impossible de récupérer le statut de verrouillage de %s: %s",
                    charger_id,
                    err,
                )
                continue
            charger_info["lock_enabled"] = lock_status.get("enabled")
            charger_info["lock_interactable"] = lock_status.get("interactable")

        # Complète chaque borne avec sa dernière transaction ("last_transaction",
        # potentiellement en cours) ET la dernière transaction terminée
        # ("last_completed_transaction", toujours avec un stopped_at) : les
        # deux peuvent différer si une nouvelle session a démarré avant que
        # l'ancienne soit consultée. Un seul appel API par borne (la liste
        # complète), traité localement pour extraire les deux.
        for charger_id, charger_info in result.items():
            try:
                transactions = await self._client.async_list_charger_transactions(charger_id)
            except PlugchoiceApiError as err:
                _LOGGER.debug(
                    "Impossible de récupérer les transactions de %s: %s", charger_id, err
                )
                continue

            if not transactions:
                charger_info["last_transaction"] = None
                charger_info["last_completed_transaction"] = None
                continue

            transactions.sort(key=lambda t: t.get("started_at") or "", reverse=True)
            charger_info["last_transaction"] = transactions[0]

            completed = [t for t in transactions if t.get("stopped_at")]
            charger_info["last_completed_transaction"] = completed[0] if completed else None

        # Complète chaque borne avec le dernier profil de charge (limite)
        # appliqué, lu depuis les logs OCPP. Erreur isolée par borne comme
        # pour les autres enrichissements.
        for charger_id, charger_info in result.items():
            try:
                charger_info["charging_profile"] = (
                    await self._client.async_get_latest_charging_profile(charger_id)
                )
            except PlugchoiceApiError as err:
                _LOGGER.debug(
                    "Impossible de récupérer le profil de charge de %s: %s", charger_id, err
                )
                continue

        # Reconstruit l'annuaire des badges à partir des cartes RFID
        # enregistrées sur Plugchoice, site par site (les cartes sont
        # rattachées à un site, pas à une borne précise).
        directory: dict[str, str] = {}
        try:
            sites = await self._client.async_list_sites()
        except PlugchoiceApiError as err:
            _LOGGER.debug("Impossible de lister les sites pour les badges: %s", err)
            sites = []

        for site in sites:
            site_id = site.get("uuid")
            if not site_id:
                continue
            try:
                cards = await self._client.async_list_cards(site_id)
            except PlugchoiceApiError as err:
                # Cause fréquente : droits "sensitive" non accordés sur ce
                # site pour ce token (cf. permissions.sensitive de l'API
                # Locations) — on log en debug et on continue les autres sites.
                _LOGGER.debug(
                    "Impossible de lister les badges du site %s: %s", site_id, err
                )
                continue
            for card in cards:
                id_token = card.get("id_token")
                name = card.get("name")
                if id_token and name:
                    directory[id_token] = name

        self.badge_directory = directory

        return result


class PlugchoiceBadgeEnergyCoordinator(DataUpdateCoordinator[dict[str, float]]):
    """Agrège l'énergie cumulée consommée par badge, toutes bornes/sessions confondues.

    Expose un dict {badge_id: kwh_cumulés}. La valeur ne peut que croître
    (nouvelle transaction = ajout), ce qui la rend directement utilisable
    comme source "total_increasing" dans le tableau Énergie de Home
    Assistant. Parcourt tout l'historique des transactions à chaque cycle :
    volontairement plus espacé que les autres coordinators (cf.
    BADGE_ENERGY_INTERVAL) pour limiter le nombre d'appels API.

    Le total exposé pour un badge n'est jamais autorisé à diminuer d'un
    cycle à l'autre (on conserve le maximum vu) : un cycle où une borne
    répond en erreur ne compterait qu'une partie des transactions, et une
    baisse ponctuelle serait interprétée par Home Assistant comme une
    remise à zéro de compteur (faux pic dans le tableau Énergie). Si
    AUCUNE borne ne répond, on lève UpdateFailed plutôt que d'exposer des
    données tronquées.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: PlugchoiceClient,
        chargers_coordinator: PlugchoiceChargersCoordinator,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_badge_energy",
            update_interval=BADGE_ENERGY_INTERVAL,
        )
        self._client = client
        self._chargers_coordinator = chargers_coordinator
        # Plus haut total jamais exposé pour chaque badge (cliquet anti-retour).
        self._cumulative: dict[str, float] = {}

    async def _async_update_data(self) -> dict[str, float]:
        charger_ids = list(self._chargers_coordinator.data.keys())
        totals: dict[str, float] = {}
        any_success = False

        for charger_id in charger_ids:
            try:
                transactions = await self._client.async_list_charger_transactions(charger_id)
            except PlugchoiceApiError as err:
                _LOGGER.debug(
                    "Impossible de lister les transactions de %s pour l'énergie par badge: %s",
                    charger_id,
                    err,
                )
                continue

            any_success = True
            for transaction in transactions:
                badge_id = transaction.get("id_tag")
                kwh = transaction.get("total_kwh")
                if not badge_id or kwh is None:
                    continue
                try:
                    kwh_value = float(kwh)
                except (TypeError, ValueError):
                    continue
                totals[badge_id] = totals.get(badge_id, 0.0) + kwh_value

        if charger_ids and not any_success:
            raise UpdateFailed(
                "Aucune borne n'a répondu pour l'agrégation d'énergie par badge"
            )

        # Cliquet : on ne laisse jamais un total redescendre (cf. docstring).
        for badge_id, value in totals.items():
            self._cumulative[badge_id] = max(self._cumulative.get(badge_id, 0.0), value)

        return dict(self._cumulative)


class PlugchoiceMeterCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Interroge Plugchoice et expose les dernières valeurs par clé de capteur."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: PlugchoiceClient,
        charger_id: str,
        scan_interval_seconds: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{charger_id}",
            update_interval=timedelta(seconds=scan_interval_seconds),
        )
        self._client = client
        self.charger_id = charger_id

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            raw_values = await self._client.async_get_latest_meter_values(self.charger_id)
        except PlugchoiceAuthError as err:
            raise UpdateFailed(f"Authentification Plugchoice refusée: {err}") from err
        except PlugchoiceApiError as err:
            raise UpdateFailed(f"Erreur API Plugchoice: {err}") from err

        # On trie par timestamp croissant puis on ne garde que la dernière
        # valeur connue pour chaque (measurand, phase) -> reflète l'état "live".
        raw_values.sort(key=lambda item: item.get("timestamp", ""))

        latest: dict[str, Any] = {}
        latest_timestamp: str | None = None

        for item in raw_values:
            key = (item.get("measurand"), item.get("phase"))
            sensor_key = SENSOR_MAP.get(key)
            if sensor_key is None:
                continue
            try:
                value = float(item["value"])
            except (TypeError, ValueError):
                continue
            latest[sensor_key] = value
            latest_timestamp = item.get("timestamp", latest_timestamp)

        latest["last_update"] = latest_timestamp
        return latest
