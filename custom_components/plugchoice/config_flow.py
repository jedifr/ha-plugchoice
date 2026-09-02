"""Config flow pour Plugchoice.

Étapes :
1. "user" : token, validation, découverte des bornes.
2. "badges" : association optionnelle badge RFID -> nom lisible (répétable,
   on peut en ajouter plusieurs avant de terminer). Modifiable ensuite à
   tout moment via le bouton "Configurer" de l'intégration (options flow).
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_TOKEN
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import PlugchoiceApiError, PlugchoiceAuthError, PlugchoiceClient
from .const import (
    CONF_BADGE_NAMES,
    CONF_BADGE_PRIORITIES,
    CONF_GRID_POWER_ENTITY,
    CONF_LOAD_BALANCING_ENABLED,
    CONF_LOAD_BALANCING_WINDOW,
    CONF_MAX_GRID_POWER,
    CONF_SCAN_INTERVAL,
    DEFAULT_BADGE_PRIORITY,
    DEFAULT_LOAD_BALANCING_WINDOW_SECONDS,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    MAX_BADGE_PRIORITY,
    MAX_LOAD_BALANCING_WINDOW_SECONDS,
    MIN_BADGE_PRIORITY,
    MIN_CHARGING_CURRENT,
    MIN_LOAD_BALANCING_WINDOW_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema({vol.Required(CONF_TOKEN): str})

BADGES_SCHEMA = vol.Schema(
    {
        vol.Optional("badge_id"): str,
        vol.Optional("badge_name"): str,
        vol.Optional("remove_badge_id"): str,
        vol.Optional("finish", default=False): bool,
    }
)


def _format_badge_list(badge_names: dict[str, str]) -> str:
    """Formatte le mapping courant pour l'affichage dans le formulaire."""
    if not badge_names:
        return "Aucune surcharge pour l'instant (les noms sont détectés automatiquement depuis Plugchoice)."
    return "\n".join(f"• {badge_id} → {name}" for badge_id, name in badge_names.items())


def _format_priority_list(priorities: dict[str, dict[str, Any]], names: dict[str, str]) -> str:
    """Formatte les priorités courantes (avec nom si connu) pour l'affichage dans le formulaire."""
    if not priorities:
        return f"Aucune priorité définie (tous les badges partagent la priorité par défaut : {DEFAULT_BADGE_PRIORITY})."
    lines = []
    for badge_id, config in priorities.items():
        label = names.get(badge_id, badge_id)
        if config.get("unlimited"):
            lines.append(f"• {label} → priorité absolue (budget partagé ignoré)")
            continue
        priority = config.get("priority", DEFAULT_BADGE_PRIORITY)
        max_amps = config.get("max_amps")
        if max_amps is not None:
            lines.append(f"• {label} → priorité {priority}, plafond {max_amps}A")
        else:
            lines.append(f"• {label} → priorité {priority}")
    return "\n".join(lines)


class PlugchoiceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Gère la configuration initiale de l'intégration (un token = un compte)."""

    VERSION = 1

    def __init__(self) -> None:
        self._token: str = ""
        self._account_title: str = "Plugchoice"
        self._badge_names: dict[str, str] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            session = async_get_clientsession(self.hass)
            client = PlugchoiceClient(session, token)

            try:
                user = await client.async_get_user()
                chargers = await client.async_list_chargers()
            except PlugchoiceAuthError:
                errors["base"] = "invalid_auth"
            except PlugchoiceApiError:
                errors["base"] = "unknown"
            else:
                if not chargers:
                    errors["base"] = "no_chargers_found"
                else:
                    # Un token = une entrée unique. Si le même token est
                    # ré-ajouté, on abandonne plutôt que de dupliquer les
                    # appareils (utile si l'utilisateur régénère et confond
                    # ajout / mise à jour du token).
                    # uuid/email en priorité ; en dernier recours un hash
                    # du token (jamais le token en clair comme identifiant).
                    unique_id = (
                        user.get("uuid")
                        or user.get("email")
                        or hashlib.sha256(token.encode()).hexdigest()[:16]
                    )
                    await self.async_set_unique_id(unique_id)
                    self._abort_if_unique_id_configured()

                    self._token = token
                    self._account_title = user.get("name") or "Plugchoice"
                    return await self.async_step_badges()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_badges(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Étape optionnelle et répétable: nommer des badges RFID connus.

        On peut laisser vide et passer directement à "Terminer" — les
        badges pourront toujours être nommés plus tard, un par un, via
        les options de l'intégration.
        """
        if user_input is not None:
            badge_id = (user_input.get("badge_id") or "").strip()
            badge_name = (user_input.get("badge_name") or "").strip()
            if badge_id and badge_name:
                self._badge_names[badge_id] = badge_name

            remove_id = (user_input.get("remove_badge_id") or "").strip()
            if remove_id:
                self._badge_names.pop(remove_id, None)

            if user_input.get("finish"):
                return self.async_create_entry(
                    title=self._account_title,
                    data={CONF_TOKEN: self._token},
                    options={CONF_BADGE_NAMES: self._badge_names},
                )

        return self.async_show_form(
            step_id="badges",
            data_schema=BADGES_SCHEMA,
            description_placeholders={"current_badges": _format_badge_list(self._badge_names)},
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> PlugchoiceOptionsFlow:
        return PlugchoiceOptionsFlow(config_entry)


class PlugchoiceOptionsFlow(config_entries.OptionsFlow):
    """Menu d'options: connexion (token/intervalle) ou gestion des badges nommés."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._token: str = config_entry.data.get(CONF_TOKEN, "")
        self._scan_interval: int = config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS
        )
        self._badge_names: dict[str, str] = dict(config_entry.options.get(CONF_BADGE_NAMES, {}))
        self._badge_priorities: dict[str, dict[str, Any]] = {
            badge_id: dict(config)
            for badge_id, config in config_entry.options.get(CONF_BADGE_PRIORITIES, {}).items()
        }
        self._load_balancing: dict[str, Any] = {
            CONF_LOAD_BALANCING_ENABLED: config_entry.options.get(
                CONF_LOAD_BALANCING_ENABLED, False
            ),
            CONF_GRID_POWER_ENTITY: config_entry.options.get(CONF_GRID_POWER_ENTITY),
            CONF_MAX_GRID_POWER: config_entry.options.get(CONF_MAX_GRID_POWER, 9000),
            CONF_LOAD_BALANCING_WINDOW: config_entry.options.get(
                CONF_LOAD_BALANCING_WINDOW, DEFAULT_LOAD_BALANCING_WINDOW_SECONDS
            ),
        }

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["connection", "badges", "load_balancing", "priorities", "finish"],
        )

    def _domain_data(self) -> dict[str, Any]:
        return self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id, {})

    def _known_badges(self) -> dict[str, str]:
        """Construit {badge_id: libellé affiché} à partir de toutes les sources connues.

        Combine les cartes RFID enregistrées sur Plugchoice (annuaire
        auto-détecté), les badges déjà vus dans l'historique des
        transactions (via le coordinator d'énergie par badge), et les
        surcharges de nom manuelles — pour proposer une liste déroulante
        plutôt que de faire retaper un ID à la main.
        """
        domain_data = self._domain_data()
        chargers_coordinator = domain_data.get("chargers_coordinator")
        badge_energy_coordinator = domain_data.get("badge_energy_coordinator")

        auto_directory: dict[str, str] = {}
        if chargers_coordinator is not None:
            auto_directory = chargers_coordinator.badge_directory or {}

        ids: set[str] = set(auto_directory.keys())
        if badge_energy_coordinator is not None:
            ids.update((badge_energy_coordinator.data or {}).keys())
        ids.update(self._badge_names.keys())
        ids.update(self._badge_priorities.keys())

        return {
            badge_id: (
                f"{name} ({badge_id})"
                if (name := self._badge_names.get(badge_id) or auto_directory.get(badge_id))
                else badge_id
            )
            for badge_id in ids
        }

    def _badge_select_options(self, ids: set[str] | None = None) -> list[SelectOptionDict]:
        """Options de liste déroulante, triées par libellé. Restreint à `ids` si fourni."""
        known = self._known_badges()
        selected_ids = ids if ids is not None else set(known.keys())
        return [
            SelectOptionDict(value=badge_id, label=known.get(badge_id, badge_id))
            for badge_id in sorted(selected_ids, key=lambda i: known.get(i, i))
        ]

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            session = async_get_clientsession(self.hass)
            client = PlugchoiceClient(session, token)
            try:
                await client.async_get_user()
            except PlugchoiceAuthError:
                errors["base"] = "invalid_auth"
            except PlugchoiceApiError:
                errors["base"] = "unknown"
            else:
                self._token = token
                self._scan_interval = user_input[CONF_SCAN_INTERVAL]
                return await self.async_step_init()

        return self.async_show_form(
            step_id="connection",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TOKEN, default=self._token): str,
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=self._scan_interval
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL_SECONDS, max=3600, unit_of_measurement="s"
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_badges(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            badge_id = (user_input.get("badge_id") or "").strip()
            badge_name = (user_input.get("badge_name") or "").strip()
            if badge_id and badge_name:
                self._badge_names[badge_id] = badge_name

            remove_id = (user_input.get("remove_badge_id") or "").strip()
            if remove_id:
                self._badge_names.pop(remove_id, None)

            if user_input.get("finish"):
                return await self.async_step_init()

        # "badge_id" propose tous les badges déjà vus (nommés ou non) ;
        # custom_value=True permet aussi de saisir un ID pas encore connu.
        all_options = self._badge_select_options()
        remove_options = self._badge_select_options(set(self._badge_names.keys()))

        schema_dict: dict[Any, Any] = {
            vol.Optional("badge_id"): SelectSelector(
                SelectSelectorConfig(
                    options=all_options, mode=SelectSelectorMode.DROPDOWN, custom_value=True
                )
            ),
            vol.Optional("badge_name"): str,
        }
        if remove_options:
            schema_dict[vol.Optional("remove_badge_id")] = SelectSelector(
                SelectSelectorConfig(options=remove_options, mode=SelectSelectorMode.DROPDOWN)
            )
        schema_dict[vol.Optional("finish", default=False)] = bool

        return self.async_show_form(
            step_id="badges",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={"current_badges": _format_badge_list(self._badge_names)},
        )

    async def async_step_load_balancing(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Configure le régulateur : capteur de puissance, budget max, fenêtre de lissage.

        Le budget est partagé entre toutes les bornes découvertes (pas un
        budget par borne), et ajusté à la hausse comme à la baisse selon
        la marge disponible (capteur négatif = injection réseau).
        """
        if user_input is not None:
            self._load_balancing[CONF_LOAD_BALANCING_ENABLED] = user_input[
                CONF_LOAD_BALANCING_ENABLED
            ]
            self._load_balancing[CONF_GRID_POWER_ENTITY] = user_input.get(CONF_GRID_POWER_ENTITY)
            self._load_balancing[CONF_MAX_GRID_POWER] = user_input[CONF_MAX_GRID_POWER]
            self._load_balancing[CONF_LOAD_BALANCING_WINDOW] = user_input[
                CONF_LOAD_BALANCING_WINDOW
            ]
            return await self.async_step_init()

        current_entity = self._load_balancing.get(CONF_GRID_POWER_ENTITY)
        entity_field = (
            vol.Optional(CONF_GRID_POWER_ENTITY, default=current_entity)
            if current_entity
            else vol.Optional(CONF_GRID_POWER_ENTITY)
        )

        return self.async_show_form(
            step_id="load_balancing",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LOAD_BALANCING_ENABLED,
                        default=self._load_balancing[CONF_LOAD_BALANCING_ENABLED],
                    ): bool,
                    entity_field: EntitySelector(EntitySelectorConfig(domain="sensor")),
                    vol.Required(
                        CONF_MAX_GRID_POWER,
                        default=self._load_balancing[CONF_MAX_GRID_POWER],
                    ): NumberSelector(
                        NumberSelectorConfig(min=0, max=100000, unit_of_measurement="W")
                    ),
                    vol.Required(
                        CONF_LOAD_BALANCING_WINDOW,
                        default=self._load_balancing[CONF_LOAD_BALANCING_WINDOW],
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_LOAD_BALANCING_WINDOW_SECONDS,
                            max=MAX_LOAD_BALANCING_WINDOW_SECONDS,
                            unit_of_measurement="s",
                        )
                    ),
                }
            ),
        )

    async def async_step_priorities(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Définit la priorité (et un plafond optionnel) par badge, pour le load balancing.

        Utilisé uniquement quand plusieurs bornes chargent en même temps
        avec un budget partagé : le badge le plus prioritaire est rempli
        en premier au-delà du minimum garanti pour tous.
        """
        if user_input is not None:
            badge_id = (user_input.get("badge_id") or "").strip()
            if badge_id:
                config: dict[str, Any] = {"priority": int(user_input.get("priority", DEFAULT_BADGE_PRIORITY))}
                max_amps = user_input.get("max_amps")
                if max_amps is not None:
                    config["max_amps"] = float(max_amps)
                if user_input.get("unlimited"):
                    config["unlimited"] = True
                self._badge_priorities[badge_id] = config

            remove_id = (user_input.get("remove_badge_id") or "").strip()
            if remove_id:
                self._badge_priorities.pop(remove_id, None)

            if user_input.get("finish"):
                return await self.async_step_init()

        all_options = self._badge_select_options()
        remove_options = self._badge_select_options(set(self._badge_priorities.keys()))

        schema_dict: dict[Any, Any] = {
            vol.Optional("badge_id"): SelectSelector(
                SelectSelectorConfig(
                    options=all_options, mode=SelectSelectorMode.DROPDOWN, custom_value=True
                )
            ),
            vol.Optional("priority", default=DEFAULT_BADGE_PRIORITY): NumberSelector(
                NumberSelectorConfig(min=MIN_BADGE_PRIORITY, max=MAX_BADGE_PRIORITY, step=1)
            ),
            vol.Optional("max_amps"): NumberSelector(
                NumberSelectorConfig(min=MIN_CHARGING_CURRENT, max=63, unit_of_measurement="A")
            ),
            vol.Optional("unlimited", default=False): bool,
        }
        if remove_options:
            schema_dict[vol.Optional("remove_badge_id")] = SelectSelector(
                SelectSelectorConfig(options=remove_options, mode=SelectSelectorMode.DROPDOWN)
            )
        schema_dict[vol.Optional("finish", default=False)] = bool

        return self.async_show_form(
            step_id="priorities",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "current_priorities": _format_priority_list(
                    self._badge_priorities, self._known_badges()
                )
            },
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        new_data = {**self._config_entry.data, CONF_TOKEN: self._token}
        self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
        return self.async_create_entry(
            title="",
            data={
                CONF_SCAN_INTERVAL: self._scan_interval,
                CONF_BADGE_NAMES: self._badge_names,
                CONF_BADGE_PRIORITIES: self._badge_priorities,
                **self._load_balancing,
            },
        )
