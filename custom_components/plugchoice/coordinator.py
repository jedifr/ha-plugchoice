"""Coordinators Plugchoice.

Deux niveaux :
- PlugchoiceChargersCoordinator : liste périodiquement les bornes du compte
  (découverte automatique, y compris de nouvelles bornes ajoutées plus tard).
- PlugchoiceMeterCoordinator : un par borne, interroge ses relevés de compteur.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PlugchoiceApiError, PlugchoiceAuthError, PlugchoiceClient
from .const import (
    ACTIVE_CHARGING_POWER_THRESHOLD,
    BADGE_ENERGY_INTERVAL,
    DISCOVERY_INTERVAL,
    DOMAIN,
    METER_IDLE_INTERVAL_MULTIPLIER,
    SENSOR_MAP,
)

_LOGGER = logging.getLogger(__name__)

# Statuts OCPP (StatusNotification) sur lesquels un démarrage à distance n'a
# pas de sens : la borne le refusera (ou ce serait sans effet).
NON_STARTABLE_CONNECTOR_STATUSES = frozenset(
    {"charging", "suspendedev", "finishing", "faulted", "unavailable", "reserved"}
)


def connector_status(charger_info: dict[str, Any]) -> str | None:
    """Extrait le statut OCPP du (premier) connecteur d'une borne.

    L'emplacement exact du champ n'étant pas documenté côté Plugchoice, on
    tente plusieurs formes : liste `connectors`, objet `connector`, ou champ
    de statut au niveau de la borne. Retourne la chaîne brute (ex.
    "Available", "SuspendedEV") ou None si introuvable.
    """
    detail = charger_info.get("_detail") or {}
    for source in (charger_info, detail):
        connectors = source.get("connectors")
        if isinstance(connectors, list) and connectors:
            first = connectors[0]
            if isinstance(first, dict):
                status = first.get("status") or first.get("state")
                if status:
                    return str(status)
        connector = source.get("connector")
        if isinstance(connector, dict):
            status = connector.get("status") or connector.get("state")
            if status:
                return str(status)
        for key in ("connector_status", "ocpp_status", "status", "state"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def connector_error_code(charger_info: dict[str, Any]) -> str | None:
    """Code d'erreur OCPP du connecteur, si exposé (ex. "NoError", "GroundFailure")."""
    detail = charger_info.get("_detail") or {}
    for source in (charger_info, detail):
        connectors = source.get("connectors")
        if isinstance(connectors, list) and connectors and isinstance(connectors[0], dict):
            code = connectors[0].get("error_code") or connectors[0].get("errorCode")
            if code:
                return str(code)
    return None


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

        # Enrichit chaque borne (Plug & Charge, verrouillage, transactions,
        # profil de charge). Les 4 appels d'une borne sont lancés en
        # parallèle, et toutes les bornes sont traitées en parallèle : sur
        # un compte à plusieurs bornes, ça évite de sérialiser des dizaines
        # d'appels bloquants à chaque cycle de découverte.
        await asyncio.gather(
            *(self._enrich_charger(cid, info) for cid, info in result.items())
        )

        self.badge_directory = await self._build_badge_directory()

        return result

    async def _enrich_charger(
        self, charger_id: str, charger_info: dict[str, Any]
    ) -> None:
        """Complète charger_info avec les données annexes d'une borne.

        Chaque appel est isolé : une borne qui ne supporte pas une
        fonctionnalité (ou une 5xx transitoire de l'API) ne doit pas
        empêcher les autres données d'être récupérées. Si TOUS les appels
        échouent, on le signale une fois en warning (les entités passeront
        sinon à "indisponible" sans cause visible).
        """
        detail, plug_charge, lock_status, transactions, charging_profile = await asyncio.gather(
            self._client.async_get_charger(charger_id),
            self._client.async_get_plug_charge_status(charger_id),
            self._client.async_get_lock_status(charger_id),
            self._client.async_list_charger_transactions(charger_id),
            self._client.async_get_latest_charging_profile(charger_id),
            return_exceptions=True,
        )

        succeeded = 0

        if isinstance(detail, dict):
            # Objet borne complet (peut contenir l'état des connecteurs,
            # absent de la liste /chargers). Rangé à part pour ne pas
            # écraser les clés déjà enrichies.
            charger_info["_detail"] = detail
            succeeded += 1
        elif isinstance(detail, Exception):
            _LOGGER.debug("Détail borne %s indisponible: %s", charger_id, detail)

        if isinstance(plug_charge, dict):
            charger_info["current_card"] = plug_charge.get("current_card")
            charger_info["plug_charge_enabled"] = plug_charge.get("enabled")
            succeeded += 1
        elif isinstance(plug_charge, Exception):
            _LOGGER.debug("Plug & Charge %s indisponible: %s", charger_id, plug_charge)

        if isinstance(lock_status, dict):
            charger_info["lock_enabled"] = lock_status.get("enabled")
            charger_info["lock_interactable"] = lock_status.get("interactable")
            succeeded += 1
        elif isinstance(lock_status, Exception):
            _LOGGER.debug("Verrouillage %s indisponible: %s", charger_id, lock_status)

        if isinstance(transactions, list):
            succeeded += 1
            # Conservé pour PlugchoiceBadgeEnergyCoordinator, qui réutilise
            # cette liste au lieu de re-paginer l'historique via l'API.
            charger_info["transactions"] = transactions
            ordered = sorted(
                transactions, key=lambda t: t.get("started_at") or "", reverse=True
            )
            charger_info["last_transaction"] = ordered[0] if ordered else None
            completed = [t for t in ordered if t.get("stopped_at")]
            charger_info["last_completed_transaction"] = completed[0] if completed else None
        elif isinstance(transactions, Exception):
            _LOGGER.debug("Transactions %s indisponibles: %s", charger_id, transactions)

        if isinstance(charging_profile, dict) or charging_profile is None:
            charger_info["charging_profile"] = charging_profile
            succeeded += 1
        elif isinstance(charging_profile, Exception):
            _LOGGER.debug("Profil de charge %s indisponible: %s", charger_id, charging_profile)

        if succeeded == 0:
            _LOGGER.warning(
                "Aucune donnée annexe récupérée pour la borne %s ce cycle "
                "(API Plugchoice en erreur ?)",
                charger_id,
            )

    async def _build_badge_directory(self) -> dict[str, str]:
        """Reconstruit l'annuaire {id_token: nom} à partir des cartes RFID des sites."""
        try:
            sites = await self._client.async_list_sites()
        except PlugchoiceApiError as err:
            _LOGGER.debug("Impossible de lister les sites pour les badges: %s", err)
            return self.badge_directory

        site_ids = [s.get("uuid") for s in sites if s.get("uuid")]
        cards_per_site = await asyncio.gather(
            *(self._client.async_list_cards(site_id) for site_id in site_ids),
            return_exceptions=True,
        )

        directory: dict[str, str] = {}
        for site_id, cards in zip(site_ids, cards_per_site):
            if isinstance(cards, Exception):
                # Cause fréquente : droits "sensitive" non accordés sur ce
                # site pour ce token (cf. permissions.sensitive de l'API).
                _LOGGER.debug("Badges du site %s indisponibles: %s", site_id, cards)
                continue
            for card in cards:
                id_token = card.get("id_token")
                name = card.get("name")
                if id_token and name:
                    directory[id_token] = name

        # Si ce cycle n'a rien ramené (toutes les requêtes cards en erreur)
        # mais qu'on avait déjà un annuaire, on le conserve.
        if not directory and self.badge_directory:
            return self.badge_directory
        return directory


class PlugchoiceBadgeEnergyCoordinator(DataUpdateCoordinator[dict[str, float]]):
    """Agrège l'énergie cumulée consommée par badge, toutes bornes/sessions confondues.

    Expose un dict {badge_id: kwh_cumulés}. La valeur ne peut que croître
    (nouvelle transaction = ajout), ce qui la rend directement utilisable
    comme source "total_increasing" dans le tableau Énergie de Home
    Assistant.

    Ne fait AUCUN appel API : réutilise les listes de transactions déjà
    récupérées par PlugchoiceChargersCoordinator (clé "transactions" de
    chaque borne). L'intervalle propre reste plus espacé (cf.
    BADGE_ENERGY_INTERVAL) car l'agrégation elle-même est un peu coûteuse.

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
        chargers_coordinator: PlugchoiceChargersCoordinator,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_badge_energy",
            update_interval=BADGE_ENERGY_INTERVAL,
        )
        self._chargers_coordinator = chargers_coordinator
        # Plus haut total jamais exposé pour chaque badge (cliquet anti-retour).
        self._cumulative: dict[str, float] = {}

    async def _async_update_data(self) -> dict[str, float]:
        chargers = self._chargers_coordinator.data or {}
        totals: dict[str, float] = {}
        any_success = False

        for charger_info in chargers.values():
            transactions = charger_info.get("transactions")
            if transactions is None:
                # Le chargers_coordinator n'a pas pu récupérer l'historique
                # de cette borne à son dernier cycle : on l'ignore ici
                # plutôt que de compter une valeur partielle.
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

        if chargers and not any_success:
            raise UpdateFailed(
                "Aucun historique de transactions disponible pour l'énergie par badge"
            )

        # Cliquet : on ne laisse jamais un total redescendre (cf. docstring).
        for badge_id, value in totals.items():
            self._cumulative[badge_id] = max(self._cumulative.get(badge_id, 0.0), value)

        return dict(self._cumulative)


class PlugchoiceMeterCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Interroge Plugchoice et expose les dernières valeurs par clé de capteur.

    L'intervalle est adaptatif : au rythme configuré quand la borne charge
    activement, mais ralenti (x METER_IDLE_INTERVAL_MULTIPLIER) quand elle
    est au repos — inutile d'interroger toutes les 60 s une borne sans
    session. Une borne repérée au repos garde donc jusqu'à
    scan_interval x multiplicateur de latence avant que le début d'une
    nouvelle session n'apparaisse : compromis volontaire pour tenir sous
    la limite de requêtes/h de l'API sur les comptes à plusieurs bornes.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: PlugchoiceClient,
        charger_id: str,
        scan_interval_seconds: int,
    ) -> None:
        self._active_interval = timedelta(seconds=scan_interval_seconds)
        self._idle_interval = timedelta(
            seconds=scan_interval_seconds * METER_IDLE_INTERVAL_MULTIPLIER
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{charger_id}",
            update_interval=self._active_interval,
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

        # Ajuste la cadence du prochain cycle selon l'activité mesurée.
        power = latest.get("power")
        is_active = power is not None and power > ACTIVE_CHARGING_POWER_THRESHOLD
        self.update_interval = self._active_interval if is_active else self._idle_interval

        return latest
