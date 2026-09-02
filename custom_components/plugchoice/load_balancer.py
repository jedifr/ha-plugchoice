"""Load balancing Plugchoice : ajuste dynamiquement les limites de charge.

Principe : un capteur HA de puissance réseau (positif = tirage,
négatif = injection) est lissé sur une fenêtre glissante configurable.
À chaque cycle, le budget disponible pour la recharge est calculé comme :

    budget_ev = max_grid_power - (puissance_réseau_lissée - puissance_ev_actuelle)

Ce budget est ensuite réparti entre les bornes actuellement en charge en
deux passes :
1. Un minimum garanti (MIN_CHARGING_CURRENT) est alloué à chaque borne
   active, dans la limite du budget disponible.
2. Le reliquat est distribué en cascade, badge le plus prioritaire
   d'abord, chacun étant rempli jusqu'à son plafond (le plus bas entre le
   courant max de la borne et un plafond éventuel propre au badge) avant
   de passer au suivant.

Le résultat est converti en A (via la tension et le nombre de phases
connus de chaque borne, ou des valeurs par défaut sinon) et envoyé via
l'action "charge-limit" — seulement si l'écart avec la dernière valeur
envoyée dépasse un seuil, pour éviter de spammer l'API.

Le budget est donc partagé entre toutes les bornes (pas un budget par
borne), ajusté dans les deux sens (hausse comme baisse), et réparti selon
la priorité du badge qui charge sur chaque borne.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

from .api import PlugchoiceApiError, PlugchoiceClient
from .const import (
    ACTIVE_CHARGING_POWER_THRESHOLD,
    CONF_BADGE_PRIORITIES,
    CONF_GRID_POWER_ENTITY,
    CONF_LOAD_BALANCING_WINDOW,
    CONF_MAX_GRID_POWER,
    DEFAULT_ASSUMED_PHASES,
    DEFAULT_ASSUMED_VOLTAGE,
    DEFAULT_BADGE_PRIORITY,
    DEFAULT_CONNECTOR_ID,
    DEFAULT_LOAD_BALANCING_WINDOW_SECONDS,
    DEFAULT_MAX_CHARGING_CURRENT,
    LOAD_BALANCING_EVAL_INTERVAL,
    LOAD_BALANCING_MIN_CURRENT_DELTA,
    LOAD_BALANCING_PROFILE_REFRESH_SECONDS,
    MIN_CHARGING_CURRENT,
    PHASE_ACTIVE_CURRENT_THRESHOLD,
    SIGNAL_LOAD_BALANCING_UPDATE,
)
from .coordinator import PlugchoiceChargersCoordinator, PlugchoiceMeterCoordinator

_LOGGER = logging.getLogger(__name__)

EnsureMeterCoordinator = Callable[[str], Awaitable[PlugchoiceMeterCoordinator]]


@dataclass
class _ActiveCharger:
    """Infos consolidées pour une borne active, utilisées pour la répartition."""

    charger_id: str
    voltage: float
    phases: int
    max_current: float  # déjà plafonné par le badge le cas échéant
    raw_max_current: float  # jamais plafonné par un badge : vrai max matériel
    priority: int
    badge_id: str | None
    exempt: bool = False  # True : reçoit son max, exclue du calcul de partage


class PlugchoiceLoadBalancer:
    """Écoute un capteur de puissance et ajuste les limites de charge en conséquence."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: PlugchoiceClient,
        chargers_coordinator: PlugchoiceChargersCoordinator,
        ensure_meter_coordinator: EnsureMeterCoordinator,
        boosted_chargers: set[str],
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._client = client
        self._chargers_coordinator = chargers_coordinator
        self._ensure_meter_coordinator = ensure_meter_coordinator
        # Ensemble partagé (même objet que celui manipulé par switch.py) des
        # bornes actuellement en mode "Boost" manuel : exemptées du partage
        # de budget, reçoivent directement leur courant maximal.
        self._boosted_chargers = boosted_chargers

        # (timestamp monotone, valeur en W) — fenêtre glissante des relevés
        # du capteur choisi.
        self._samples: deque[tuple[float, float]] = deque()
        self._last_sent_limits: dict[str, float] = {}
        # Horodatage (monotone) du dernier envoi réussi par borne : sert à
        # réémettre le profil avant son expiration côté Plugchoice (~3 min).
        self._last_sent_at: dict[str, float] = {}
        # Bornes actives lors du cycle précédent, pour détecter une fin de
        # session (et y couper automatiquement le mode Boost éventuel).
        self._previously_active_ids: set[str] = set()
        self._unsub_state: Callable[[], None] | None = None
        self._unsub_interval: Callable[[], None] | None = None

        # Exposés pour les capteurs diagnostic (lus via le signal dispatcher).
        self.smoothed_power: float | None = None
        self.available_ev_budget: float | None = None

    @property
    def signal(self) -> str:
        """Signal dispatcher propre à cette entrée de config."""
        return SIGNAL_LOAD_BALANCING_UPDATE.format(entry_id=self._entry.entry_id)

    def _entity_id(self) -> str | None:
        return self._entry.options.get(CONF_GRID_POWER_ENTITY)

    def _max_grid_power(self) -> float | None:
        value = self._entry.options.get(CONF_MAX_GRID_POWER)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _window_seconds(self) -> float:
        return float(
            self._entry.options.get(
                CONF_LOAD_BALANCING_WINDOW, DEFAULT_LOAD_BALANCING_WINDOW_SECONDS
            )
        )

    def _badge_priority_config(self, badge_id: str | None) -> dict[str, Any]:
        if not badge_id:
            return {}
        priorities: dict[str, Any] = self._entry.options.get(CONF_BADGE_PRIORITIES, {})
        return priorities.get(badge_id, {})

    @callback
    def async_start(self) -> None:
        """Démarre l'écoute du capteur configuré. Ne fait rien si mal configuré."""
        entity_id = self._entity_id()
        if not entity_id or self._max_grid_power() is None:
            _LOGGER.warning(
                "Load balancing activé mais mal configuré (capteur ou puissance "
                "max manquant) : régulateur non démarré."
            )
            return

        self._unsub_state = async_track_state_change_event(
            self.hass, [entity_id], self._handle_state_event
        )
        self._unsub_interval = async_track_time_interval(
            self.hass, self._handle_interval, LOAD_BALANCING_EVAL_INTERVAL
        )

        # Amorce avec la valeur déjà connue, pour ne pas attendre le
        # prochain changement d'état avant la première évaluation.
        state = self.hass.states.get(entity_id)
        if state is not None:
            self._record_sample(state.state)

    @callback
    def async_stop(self) -> None:
        """Arrête l'écoute (appelé au déchargement de l'entrée)."""
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

    @callback
    def _handle_state_event(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is not None:
            self._record_sample(new_state.state)

    def _record_sample(self, raw_value: str) -> None:
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return
        now = time.monotonic()
        self._samples.append((now, value))
        self._trim_samples(now)

    def _trim_samples(self, now: float) -> None:
        cutoff = now - self._window_seconds()
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    @callback
    def _handle_interval(self, _now: Any) -> None:
        self.hass.async_create_task(self._async_evaluate())

    def _active_badge_id(self, charger_info: dict[str, Any]) -> str | None:
        """Badge associé à la session en cours sur une borne, si connu.

        Priorité au badge de la transaction en cours (ce qui s'est
        réellement passé) ; à défaut, le badge configuré en Plug & Charge
        (ce qui démarrerait une nouvelle session sur cette borne).
        """
        transaction = charger_info.get("last_transaction") or {}
        return transaction.get("id_tag") or charger_info.get("current_card")

    def _active_phase_count(
        self, meter_data: dict[str, Any], profile: dict[str, Any] | None
    ) -> int:
        """Nombre de phases réellement utilisées par le véhicule en charge.

        Un véhicule monophasé branché sur une borne triphasée ne tire que
        sur L1 : convertir son budget (W) en courant (A) en supposant 3
        phases surestime d'un facteur 3 la puissance qu'il consomme réellement
        et le bride donc à tort (ex: L1=32 A, L2=L3=0). On compte les phases
        qui portent effectivement du courant ; repli sur le numberPhases du
        dernier profil OCPP, puis sur la valeur par défaut.
        """
        measured = sum(
            1
            for key in ("current_l1", "current_l2", "current_l3")
            if self._safe_float(meter_data.get(key), default=0.0)
            > PHASE_ACTIVE_CURRENT_THRESHOLD
        )
        if measured:
            return measured
        return max(
            self._safe_int((profile or {}).get("number_phases"), default=DEFAULT_ASSUMED_PHASES),
            1,
        )

    async def _build_active_chargers(
        self, chargers: dict[str, dict[str, Any]]
    ) -> tuple[list[_ActiveCharger], float]:
        """Construit la liste enrichie des bornes actives, et la puissance EV totale."""
        active: list[_ActiveCharger] = []
        total_ev_power = 0.0

        for charger_id, charger_info in chargers.items():
            meter_coordinator = await self._ensure_meter_coordinator(charger_id)
            meter_data = meter_coordinator.data or {}
            power = self._safe_float(meter_data.get("power"), default=0.0)
            total_ev_power += power
            if power <= ACTIVE_CHARGING_POWER_THRESHOLD:
                continue

            voltage = self._safe_float(meter_data.get("voltage_l1"), default=DEFAULT_ASSUMED_VOLTAGE)
            if voltage <= 0:
                voltage = DEFAULT_ASSUMED_VOLTAGE

            profile = charger_info.get("charging_profile") or {}
            phases = self._active_phase_count(meter_data, profile)

            charger_max = self._safe_float(
                charger_info.get("max_current"), default=DEFAULT_MAX_CHARGING_CURRENT
            )
            if charger_max <= 0:
                charger_max = DEFAULT_MAX_CHARGING_CURRENT

            badge_id = self._active_badge_id(charger_info)
            badge_config = self._badge_priority_config(badge_id)
            priority = self._safe_int(badge_config.get("priority"), default=DEFAULT_BADGE_PRIORITY)

            # Exemption du calcul de partage : soit ce badge est configuré
            # en "priorité absolue" (toujours), soit cette borne a été
            # basculée en "Boost" manuellement (ponctuel). Dans les deux
            # cas, TOUT plafond est ignoré — y compris un plafond de badge
            # (max_amps) — sinon une borne "exemptée" resterait quand même
            # bridée par un plafond destiné à limiter ce badge en temps
            # normal, ce qui viderait le Boost de son sens.
            exempt = bool(badge_config.get("unlimited")) or charger_id in self._boosted_chargers

            max_current = charger_max
            badge_max = badge_config.get("max_amps")
            if badge_max is not None and not exempt:
                badge_max_value = self._safe_float(badge_max, default=charger_max)
                max_current = min(charger_max, badge_max_value)
            # Le plafond ne descend jamais sous le minimum matériel : sinon
            # cette borne ne pourrait jamais charger du tout.
            max_current = max(max_current, MIN_CHARGING_CURRENT)

            active.append(
                _ActiveCharger(
                    charger_id=charger_id,
                    voltage=voltage,
                    phases=phases,
                    max_current=max_current,
                    raw_max_current=charger_max,
                    priority=priority,
                    badge_id=badge_id,
                    exempt=exempt,
                )
            )

        return active, total_ev_power

    async def _async_evaluate(self) -> None:
        """Recalcule le budget disponible et ajuste les bornes actives si besoin."""
        max_grid_power = self._max_grid_power()
        if max_grid_power is None or not self._samples:
            return

        self._trim_samples(time.monotonic())
        if not self._samples:
            return

        avg_grid_power = sum(value for _, value in self._samples) / len(self._samples)
        self.smoothed_power = avg_grid_power

        chargers = self._chargers_coordinator.data or {}
        active_chargers, total_ev_power = await self._build_active_chargers(chargers)

        # Fin de session détectée : une borne active au cycle précédent ne
        # l'est plus maintenant. On ne coupe le Boost manuel que si la
        # transaction est VRAIMENT terminée (stopped_at renseigné) — un
        # simple creux de puissance transitoire (pause de charge normale
        # sur certains véhicules) ne doit pas suffire, sinon le Boost se
        # coupe par erreur en pleine session toujours en cours.
        current_active_ids = {charger.charger_id for charger in active_chargers}
        ended_ids = self._previously_active_ids - current_active_ids
        boost_cleared = False
        for charger_id in ended_ids:
            if charger_id not in self._boosted_chargers:
                continue
            charger_info = chargers.get(charger_id) or {}
            transaction = charger_info.get("last_transaction") or {}
            if transaction.get("stopped_at") is None:
                # Toujours pas de fin confirmée : on laisse le Boost actif,
                # même si la puissance mesurée est momentanément retombée
                # sous le seuil "actif".
                continue
            self._boosted_chargers.discard(charger_id)
            boost_cleared = True
            _LOGGER.info(
                "Load balancing: session terminée sur %s, Boost désactivé automatiquement",
                charger_id,
            )
        self._previously_active_ids = current_active_ids

        # Puissance disponible pour l'ensemble des bornes, en plus de ce
        # qu'elles tirent déjà actuellement.
        house_power_without_ev = avg_grid_power - total_ev_power
        self.available_ev_budget = max_grid_power - house_power_without_ev

        async_dispatcher_send(self.hass, self.signal)

        if not active_chargers:
            if boost_cleared:
                # Rafraîchit pour que l'interrupteur "Boost" reflète tout
                # de suite la désactivation, sans attendre le prochain
                # cycle naturel de découverte (jusqu'à 10 min).
                await self._chargers_coordinator.async_request_refresh()
            return

        # Les bornes exemptées (boost manuel ou badge "priorité absolue")
        # reçoivent directement leur maximum, sans passer par le partage.
        # Les autres se répartissent le budget normalement — comme leur
        # consommation réelle (y compris celle des bornes exemptées) est
        # déjà comptée dans total_ev_power ci-dessus, l'effet d'un boost
        # se répercute naturellement sur le budget des cycles suivants.
        exempt_chargers = [c for c in active_chargers if c.exempt]
        normal_chargers = [c for c in active_chargers if not c.exempt]

        targets_watts: dict[str, float] = {
            charger.charger_id: charger.max_current * charger.voltage * charger.phases
            for charger in exempt_chargers
        }
        targets_watts.update(
            self._distribute_budget(normal_chargers, max(self.available_ev_budget, 0.0))
        )

        any_change_sent = boost_cleared
        for charger in active_chargers:
            # Revérifié ici (pas seulement au moment de la construction de
            # la liste ci-dessus) : si le switch "Boost" a été activé
            # pendant que ce cycle calculait déjà les parts, la cible
            # utilisée à l'envoi doit malgré tout refléter l'exemption la
            # plus à jour possible, sinon une activation concurrente au
            # calcul peut se voir écrasée par une valeur déjà obsolète.
            live_exempt = charger.exempt or charger.charger_id in self._boosted_chargers
            if live_exempt:
                target_current = round(charger.raw_max_current)
                target_current = max(
                    MIN_CHARGING_CURRENT, min(target_current, charger.raw_max_current)
                )
            else:
                target_current = round(
                    targets_watts[charger.charger_id] / (charger.voltage * charger.phases)
                )
                target_current = max(
                    MIN_CHARGING_CURRENT, min(target_current, charger.max_current)
                )
            if await self._send_if_needed(charger.charger_id, target_current):
                any_change_sent = True

        if any_change_sent:
            # Sans ça, le capteur "Profil de charge actif" (et le slider
            # "Limite de charge") ne refléteraient la nouvelle valeur
            # qu'au prochain cycle naturel de découverte (jusqu'à 10 min).
            # On ne rafraîchit que sur un vrai changement, pas sur une
            # simple réémission de maintien (même valeur).
            await self._chargers_coordinator.async_request_refresh()

    def _distribute_budget(
        self, active_chargers: list[_ActiveCharger], budget_watts: float
    ) -> dict[str, float]:
        """Répartit le budget en W : minimum garanti pour tous, puis priorité.

        Passe 1 — chaque borne active reçoit le minimum matériel
        (MIN_CHARGING_CURRENT), dans la limite du budget total disponible.
        Si le budget ne couvre même pas ce minimum pour tout le monde, il
        est quand même accordé à chacune (le matériel ne descend pas plus
        bas) — le budget peut alors être temporairement dépassé.

        Passe 2 — le reliquat est distribué borne par borne, badge le plus
        prioritaire d'abord, chacune étant remplie jusqu'à son propre
        plafond avant de passer à la suivante.
        """
        targets: dict[str, float] = {}
        remaining = budget_watts

        for charger in active_chargers:
            baseline_w = MIN_CHARGING_CURRENT * charger.voltage * charger.phases
            targets[charger.charger_id] = baseline_w
            remaining -= baseline_w

        remaining = max(remaining, 0.0)

        for charger in sorted(active_chargers, key=lambda c: c.priority, reverse=True):
            capacity_w = max(charger.max_current - MIN_CHARGING_CURRENT, 0.0) * (
                charger.voltage * charger.phases
            )
            allocate = min(remaining, capacity_w)
            targets[charger.charger_id] += allocate
            remaining -= allocate

        return targets

    async def _send_if_needed(self, charger_id: str, target_current: float) -> bool:
        """Envoie la limite si elle a changé, OU si le profil précédent va expirer.

        Plugchoice donne à chaque profil `charge-limit` une validité de
        ~3 min ; sans réémission, la borne repasse sans limite entre deux
        changements de cible. On renvoie donc la même valeur toutes les
        LOAD_BALANCING_PROFILE_REFRESH_SECONDS pour maintenir le profil actif.

        Retourne True uniquement sur un vrai changement de cible (pour ne
        déclencher un rafraîchissement du coordinator que dans ce cas) ;
        une simple réémission de maintien retourne False.
        """
        last_sent = self._last_sent_limits.get(charger_id)
        last_at = self._last_sent_at.get(charger_id)
        changed = (
            last_sent is None
            or abs(target_current - last_sent) >= LOAD_BALANCING_MIN_CURRENT_DELTA
        )
        stale = (
            last_at is None
            or (time.monotonic() - last_at) >= LOAD_BALANCING_PROFILE_REFRESH_SECONDS
        )
        _LOGGER.debug(
            "Load balancing: borne %s — cible=%sA (dernière=%sA, changement=%s, à réémettre=%s)",
            charger_id,
            target_current,
            last_sent,
            changed,
            stale,
        )
        if not changed and not stale:
            return False

        try:
            result = await self._client.async_set_charging_limit(
                charger_id, DEFAULT_CONNECTOR_ID, target_current
            )
        except PlugchoiceApiError as err:
            _LOGGER.warning(
                "Load balancing: échec de l'envoi de %sA à la borne %s: %s",
                target_current,
                charger_id,
                err,
            )
            return False

        status = str((result or {}).get("status") or "").lower()
        if status and status not in ("accepted", "ok", "success"):
            # La borne a répondu mais a refusé la commande (ex: "Rejected").
            # On ne met à jour ni la valeur ni l'horodatage, pour retenter
            # au prochain cycle plutôt que de considérer ça comme acquis.
            _LOGGER.warning(
                "Load balancing: la borne %s a refusé la limite %sA (statut: %s)",
                charger_id,
                target_current,
                status,
            )
            return False

        self._last_sent_limits[charger_id] = target_current
        self._last_sent_at[charger_id] = time.monotonic()
        _LOGGER.debug("Load balancing: %sA envoyés à la borne %s", target_current, charger_id)
        return changed

    @staticmethod
    def _safe_float(value: Any, default: float) -> float:
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value) if value is not None else default
        except (TypeError, ValueError):
            return default
