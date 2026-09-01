"""Constantes pour l'intégration Plugchoice."""
from datetime import timedelta

DOMAIN = "plugchoice"

CONF_SCAN_INTERVAL = "scan_interval"
CONF_BADGE_NAMES = "badge_names"

API_BASE_URL = "https://app.plugchoice.com/api/v3"

# Fenêtre glissante interrogée à chaque rafraîchissement pour être sûr
# de capter au moins un relevé récent, même si la borne échantillonne
# de façon espacée.
METER_VALUES_WINDOW_MINUTES = 10

# Un appel par borne toutes les 60s par défaut. Avec beaucoup de bornes,
# ça peut approcher la limite de 500 req/h de l'API (ex: 8 bornes x 60/h
# = 480 req/h) : l'options flow permet d'augmenter cet intervalle.
DEFAULT_SCAN_INTERVAL_SECONDS = 60
MIN_SCAN_INTERVAL_SECONDS = 30

# Intervalle de la liste des bornes (découverte), volontairement bien plus
# espacé : la liste des bornes d'un compte change rarement.
DISCOVERY_INTERVAL = timedelta(minutes=10)

# L'agrégation d'énergie par badge parcourt tout l'historique des
# transactions de chaque borne : plus coûteux que le reste, donc un
# intervalle nettement plus long.
BADGE_ENERGY_INTERVAL = timedelta(minutes=30)

# --- Load balancing (répartition dynamique de puissance) ---
CONF_LOAD_BALANCING_ENABLED = "load_balancing_enabled"
CONF_GRID_POWER_ENTITY = "grid_power_entity_id"
CONF_MAX_GRID_POWER = "max_grid_power_w"
CONF_LOAD_BALANCING_WINDOW = "load_balancing_window_seconds"

DEFAULT_LOAD_BALANCING_WINDOW_SECONDS = 60
MIN_LOAD_BALANCING_WINDOW_SECONDS = 5
MAX_LOAD_BALANCING_WINDOW_SECONDS = 900

# Cadence de réévaluation du régulateur, indépendante de la fréquence des
# mises à jour du capteur choisi.
LOAD_BALANCING_EVAL_INTERVAL = timedelta(seconds=15)
# On n'envoie une nouvelle limite à une borne que si l'écart avec la
# dernière valeur envoyée dépasse ce seuil (évite de spammer l'action
# charge-limit pour des variations négligeables).
LOAD_BALANCING_MIN_CURRENT_DELTA = 1  # A

# Repli si l'info n'est pas connue pour une borne donnée (cf. number.py).
MIN_CHARGING_CURRENT = 6
DEFAULT_MAX_CHARGING_CURRENT = 32
# Hypothèses par défaut pour convertir un budget en W vers un courant en A
# quand la tension/le nombre de phases réels d'une borne ne sont pas
# connus (aucun profil de charge n'a encore été observé sur cette borne).
DEFAULT_ASSUMED_VOLTAGE = 230
DEFAULT_ASSUMED_PHASES = 3
# Seuil de puissance (W) au-delà duquel une borne est considérée "en
# charge active" et donc éligible à une part du budget partagé.
ACTIVE_CHARGING_POWER_THRESHOLD = 200

# Connecteur par défaut visé par les actions de contrôle (limite, start...) :
# on suppose une seule prise par borne, cas le plus courant.
DEFAULT_CONNECTOR_ID = 1

# stackLevel OCPP utilisé pour nos commandes de limite de charge. Une
# valeur volontairement élevée : en OCPP, le profil au stackLevel le plus
# haut l'emporte sur les autres profils actifs (ex: un profil par défaut
# du site, ou un réglage fait depuis le portail/l'app à un niveau plus
# bas) — sans ça, nos commandes peuvent être silencieusement recouvertes
# peu après avoir été acceptées.
CHARGE_LIMIT_STACK_LEVEL = 10

# Signal dispatcher émis par le load balancer à chaque réévaluation, pour
# que les capteurs diagnostic (puissance lissée, budget EV) se mettent à
# jour sans dépendre d'un DataUpdateCoordinator classique.
SIGNAL_LOAD_BALANCING_UPDATE = f"{DOMAIN}_load_balancing_update_{{entry_id}}"

# Priorité par badge : {badge_id: {"priority": int, "max_amps": float|None}}.
# Une priorité plus haute est servie en premier lors du remplissage du
# budget au-delà du minimum garanti pour tous ; max_amps plafonne ce badge
# même s'il reste du budget disponible.
CONF_BADGE_PRIORITIES = "badge_priorities"
DEFAULT_BADGE_PRIORITY = 5
MIN_BADGE_PRIORITY = 1
MAX_BADGE_PRIORITY = 10

# measurand / phase -> clé de capteur interne
# (phase=None signifie une mesure non déclinée par phase, ex: puissance totale)
SENSOR_MAP = {
    ("Current.Import", "L1"): "current_l1",
    ("Current.Import", "L2"): "current_l2",
    ("Current.Import", "L3"): "current_l3",
    ("Voltage", "L1"): "voltage_l1",
    ("Voltage", "L2"): "voltage_l2",
    ("Voltage", "L3"): "voltage_l3",
    ("Power.Active.Import", None): "power",
    ("Energy.Active.Import.Register", None): "energy",
    ("Current.Offered", None): "current_offered",
}
