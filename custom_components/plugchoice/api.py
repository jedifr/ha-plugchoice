"""Client HTTP minimal pour l'API Plugchoice."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from aiohttp import ClientError, ClientSession

from .const import API_BASE_URL, CHARGE_LIMIT_STACK_LEVEL, METER_VALUES_WINDOW_MINUTES

TIMEOUT = 15


class PlugchoiceApiError(Exception):
    """Erreur générique lors d'un appel à l'API Plugchoice."""


class PlugchoiceAuthError(PlugchoiceApiError):
    """Le token fourni est invalide ou expiré."""


def _parse_log_params(raw: Any) -> dict[str, Any] | None:
    """Parse le champ "params" d'un log OCPP.

    D'après le schéma officiel, "params" est une chaîne JSON (ex: '[]'),
    pas un objet déjà structuré. Elle peut représenter soit directement
    l'objet de la requête OCPP, soit un tableau (forme brute d'appel OCPP)
    du type [MessageTypeId, UniqueId, Action, Payload] dont on extrait le
    dernier élément objet — le payload OCPP, qui vient en fin de tableau.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
    else:
        parsed = raw

    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        for item in reversed(parsed):
            if isinstance(item, dict):
                return item
    return None


class PlugchoiceClient:
    """Petit wrapper autour des endpoints Plugchoice utilisés par l'intégration."""

    def __init__(self, session: ClientSession, token: str) -> None:
        self._session = session
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    async def async_get_user(self) -> dict[str, Any]:
        """Vérifie le token en récupérant le profil courant (utilisé au config flow)."""
        return await self._request("GET", f"{API_BASE_URL}/user")

    async def async_get_charger(self, charger_id: str) -> dict[str, Any]:
        """Récupère les infos d'une borne."""
        return await self._request("GET", f"{API_BASE_URL}/chargers/{charger_id}")

    async def async_list_chargers(self) -> list[dict[str, Any]]:
        """Liste toutes les bornes accessibles avec ce token (pagine automatiquement)."""
        chargers: list[dict[str, Any]] = []
        url: str | None = f"{API_BASE_URL}/chargers"

        # L'API pagine ses résultats (cf. bloc "links"/"meta" vu sur /metervalues) ;
        # on suit "links.next" jusqu'à épuisement pour ne rien manquer.
        while url:
            data = await self._request("GET", url)
            chargers.extend(data.get("data", []))
            url = (data.get("links") or {}).get("next")

        return chargers

    async def async_get_plug_charge_status(self, charger_id: str) -> dict[str, Any]:
        """Récupère le statut Plug & Charge d'une borne (dont le badge RFID configuré)."""
        return await self._request(
            "GET", f"{API_BASE_URL}/chargers/{charger_id}/settings/plug-charge"
        )

    async def async_get_latest_transaction(self, charger_id: str) -> dict[str, Any] | None:
        """Récupère la transaction la plus récente d'une borne (dont son id_tag/badge)."""
        transactions = await self.async_list_charger_transactions(charger_id)
        if not transactions:
            return None
        transactions.sort(key=lambda t: t.get("started_at") or "", reverse=True)
        return transactions[0]

    async def async_list_charger_transactions(self, charger_id: str) -> list[dict[str, Any]]:
        """Liste tout l'historique des transactions d'une borne (pagine automatiquement).

        Utilisé pour agréger l'énergie consommée par badge (toutes sessions
        confondues), pas seulement la dernière session.
        """
        transactions: list[dict[str, Any]] = []
        url: str | None = f"{API_BASE_URL}/chargers/{charger_id}/transactions"
        while url:
            data = await self._request("GET", url)
            transactions.extend(data.get("data", []))
            url = (data.get("links") or {}).get("next")
        return transactions

    async def async_list_sites(self) -> list[dict[str, Any]]:
        """Liste tous les sites (locations) accessibles avec ce token."""
        sites: list[dict[str, Any]] = []
        url: str | None = f"{API_BASE_URL}/sites"
        while url:
            data = await self._request("GET", url)
            sites.extend(data.get("data", []))
            url = (data.get("links") or {}).get("next")
        return sites

    async def async_list_cards(self, site_id: str) -> list[dict[str, Any]]:
        """Liste les badges RFID enregistrés sur Plugchoice pour un site donné."""
        cards: list[dict[str, Any]] = []
        url: str | None = f"{API_BASE_URL}/sites/{site_id}/cards"
        while url:
            data = await self._request("GET", url)
            cards.extend(data.get("data", []))
            url = (data.get("links") or {}).get("next")
        return cards

    async def async_get_lock_status(self, charger_id: str) -> dict[str, Any]:
        """Récupère le statut de verrouillage de la borne (pas le verrou de câble physique).

        Une borne verrouillée passe "Unavailable" côté OCPP et refuse toute
        nouvelle session — c'est la fonction "Verrouiller la borne" de l'app
        Plugchoice, distincte du "Socket Lock" (verrou physique du câble).
        """
        return await self._request(
            "GET", f"{API_BASE_URL}/chargers/{charger_id}/settings/cable-lock"
        )

    async def async_set_lock(self, charger_id: str, enabled: bool) -> dict[str, Any]:
        """Verrouille (enabled=True) ou déverrouille (enabled=False) la borne."""
        return await self._request(
            "POST",
            f"{API_BASE_URL}/chargers/{charger_id}/settings/cable-lock",
            json_body={"enabled": enabled},
        )

    async def async_start_charging(
        self, charger_id: str, id_token: str, connector_id: int | None = None
    ) -> dict[str, Any]:
        """Démarre une session de charge à distance.

        id_token est requis par l'API (le badge RFID à utiliser pour
        autoriser la session, 20 caractères max).
        """
        body: dict[str, Any] = {"id_token": id_token}
        if connector_id is not None:
            body["connector_id"] = connector_id
        return await self._request(
            "POST", f"{API_BASE_URL}/chargers/{charger_id}/actions/start", json_body=body
        )

    async def async_stop_charging(
        self, charger_id: str, transaction_id: int | None = None
    ) -> dict[str, Any]:
        """Arrête une session de charge à distance.

        Sans transaction_id, la borne arrête la transaction active.
        """
        body: dict[str, Any] = {}
        if transaction_id is not None:
            body["transaction_id"] = transaction_id
        return await self._request(
            "POST", f"{API_BASE_URL}/chargers/{charger_id}/actions/stop", json_body=body
        )

    async def async_get_latest_charging_profile(self, charger_id: str) -> dict[str, Any] | None:
        """Récupère le dernier profil de charge (SetChargingProfile) appliqué à une borne.

        Lu depuis les logs OCPP bruts, pas depuis les transactions : ce sont
        deux objets différents (un profil définit une limite envoyée à la
        borne, une transaction décrit une session de charge).
        """
        data = await self._request(
            "GET",
            f"{API_BASE_URL}/chargers/{charger_id}/logs",
            params={
                "filter[method]": "SetChargingProfile",
                "sort": "-created_at",
                "per_page": 5,
            },
        )

        for log in data.get("data", []):
            # On filtre nous-même par méthode en plus du filtre serveur :
            # si "filter[method]" n'était pas le bon nom de paramètre côté
            # API, ce filtrage client reste correct (juste moins efficace).
            if log.get("method") != "SetChargingProfile":
                continue

            payload = _parse_log_params(log.get("params"))
            if not payload:
                continue
            cs_profile = payload.get("csChargingProfiles")
            if not isinstance(cs_profile, dict):
                continue

            schedule = cs_profile.get("chargingSchedule") or {}
            periods = schedule.get("chargingSchedulePeriod") or []
            first_period = periods[0] if periods else {}

            return {
                "charging_profile_id": cs_profile.get("chargingProfileId"),
                "connector_id": payload.get("connectorId"),
                "limit": first_period.get("limit"),
                "start_period": first_period.get("startPeriod"),
                "number_phases": first_period.get("numberPhases"),
                "transaction_id": cs_profile.get("transactionId"),
                "stack_level": cs_profile.get("stackLevel"),
                "charging_profile_purpose": cs_profile.get("chargingProfilePurpose"),
                "charging_profile_kind": cs_profile.get("chargingProfileKind"),
                "charging_rate_unit": schedule.get("chargingRateUnit"),
                "status": log.get("status"),
                "applied_at": log.get("created_at"),
            }

        return None

    async def async_set_charging_limit(
        self,
        charger_id: str,
        connector_id: int,
        limit: float,
        stack_level: int = CHARGE_LIMIT_STACK_LEVEL,
    ) -> dict[str, Any] | None:
        """Envoie une nouvelle limite de charge (en A) à une borne.

        Le stackLevel est volontairement élevé par défaut (voir
        CHARGE_LIMIT_STACK_LEVEL) : en OCPP, le profil au stackLevel le
        plus haut l'emporte sur les autres — sans ça, une commande
        pourtant acceptée peut se voir silencieusement recouverte par un
        profil existant à un niveau plus bas (ex: réglage fait depuis le
        portail ou l'app Plugchoice).
        """
        body: dict[str, Any] = {
            "connector_id": connector_id,
            "limit": limit,
            "stack_level": stack_level,
        }
        return await self._request(
            "POST", f"{API_BASE_URL}/chargers/{charger_id}/actions/charge-limit", json_body=body
        )

    async def async_get_latest_meter_values(self, charger_id: str) -> list[dict[str, Any]]:
        """Récupère les relevés de compteur sur une fenêtre glissante récente."""
        now = datetime.now(timezone.utc)
        date_from = (now - timedelta(minutes=METER_VALUES_WINDOW_MINUTES)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        date_to = now.strftime("%Y-%m-%d %H:%M:%S")

        data = await self._request(
            "GET",
            f"{API_BASE_URL}/chargers/{charger_id}/metervalues",
            params={"date_from": date_from, "date_to": date_to},
        )
        return data.get("data", [])

    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with asyncio.timeout(TIMEOUT):
                async with self._session.request(
                    method, url, headers=self._headers(), params=params, json=json_body
                ) as response:
                    if response.status == 401 or response.status == 403:
                        raise PlugchoiceAuthError(
                            f"Authentification refusée ({response.status})"
                        )
                    if response.status >= 400:
                        body = await response.text()
                        raise PlugchoiceApiError(
                            f"Erreur API {response.status}: {body}"
                        )
                    if response.status == 204 or not await response.text():
                        return {}
                    return await response.json()
        except TimeoutError as err:
            raise PlugchoiceApiError(f"Délai dépassé ({TIMEOUT}s) sur {method} {url}") from err
        except ClientError as err:
            raise PlugchoiceApiError(str(err)) from err
