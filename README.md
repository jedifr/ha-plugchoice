# Intégration Home Assistant — Plugchoice

[![hacs][hacs-badge]][hacs-url]
[![release][release-badge]][release-url]
[![Validate][validate-badge]][validate-url]
[![Tests][tests-badge]][tests-url]

Intégration personnalisée (`custom_component`) pour l'API Plugchoice (bornes
de recharge VE, OCPP 1.6J), avec découverte automatique, contrôle à distance
et répartition dynamique de puissance (load balancing) multi-bornes.

## Installation

### Via HACS (recommandé)

1. HACS → menu ⋮ (en haut à droite) → **Dépôts personnalisés**
2. Dépôt : `https://github.com/jedifr/ha-plugchoice` — Catégorie : **Integration** → **Ajouter**
3. Chercher **Plugchoice** dans la liste HACS → **Télécharger**
4. **Redémarrer Home Assistant**
5. **Paramètres → Appareils et services → Ajouter une intégration → Plugchoice**

[![Ouvrir dans HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=jedifr&repository=ha-plugchoice&category=integration)

### Manuellement

Copier le dossier `custom_components/plugchoice` dans le dossier
`custom_components` de la configuration Home Assistant, puis redémarrer HA et
ajouter l'intégration via **Paramètres → Appareils et services → Ajouter une
intégration → Plugchoice**.

### Configuration

Seul un **token d'accès personnel Plugchoice** est requis à l'installation —
les bornes et badges sont découverts automatiquement. Les réglages
(intervalle de rafraîchissement, badges nommés, répartition de puissance,
priorités des badges) se modifient ensuite via le bouton **Configurer** de
l'intégration.

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://github.com/hacs/integration
[release-badge]: https://img.shields.io/github/v/release/jedifr/ha-plugchoice
[release-url]: https://github.com/jedifr/ha-plugchoice/releases
[validate-badge]: https://github.com/jedifr/ha-plugchoice/actions/workflows/validate.yml/badge.svg
[validate-url]: https://github.com/jedifr/ha-plugchoice/actions/workflows/validate.yml
[tests-badge]: https://github.com/jedifr/ha-plugchoice/actions/workflows/tests.yml/badge.svg
[tests-url]: https://github.com/jedifr/ha-plugchoice/actions/workflows/tests.yml

## Architecture

| Fichier | Rôle |
|---|---|
| `api.py` | Client HTTP bas niveau vers l'API Plugchoice (`app.plugchoice.com/api/v3`). Toutes les routes utilisées y sont centralisées. |
| `const.py` | Constantes partagées (clés de config, intervalles, valeurs par défaut). |
| `coordinator.py` | Trois `DataUpdateCoordinator` : `PlugchoiceChargersCoordinator` (découverte des bornes + badges + transactions, 10 min), `PlugchoiceMeterCoordinator` (relevés temps réel par borne, 60 s, un par borne), `PlugchoiceBadgeEnergyCoordinator` (agrégation d'énergie cumulée par badge, 30 min). |
| `config_flow.py` | Flow de configuration (token) + options (connexion, badges nommés, répartition de puissance, priorités des badges). |
| `sensor.py` | Tous les capteurs : mesures temps réel, infos borne, sessions, énergie par badge, diagnostic load balancing. |
| `number.py` | Slider "Limite de charge" par borne (action `charge-limit`). |
| `lock.py` | Verrou "borne disponible/indisponible" (`settings/cable-lock`). |
| `button.py` | Boutons "Démarrer/Arrêter la charge" (`actions/start` / `actions/stop`) et "Effacer la limite de charge" (`actions/clear-charge-limit`, désactivé par défaut). |
| `select.py` | Sélecteur de badge à utiliser pour démarrer une charge. |
| `switch.py` | Interrupteur "Boost" (puissance maximale immédiate, ignore le budget partagé). |
| `load_balancer.py` | Régulateur de répartition dynamique de puissance entre bornes, piloté par un capteur HA externe (compteur réseau). |

## Fonctionnalités principales

- Découverte automatique des bornes, badges RFID (noms tirés des cartes
  Plugchoice), transactions.
- Capteurs temps réel (courants, tensions, puissance, énergie), infos borne
  (fabricant, modèle, firmware, ID, courant max), sessions (en cours /
  dernière terminée), profil de charge actif (lu depuis les logs OCPP).
- Contrôle à distance : limite de charge, verrouillage, démarrage/arrêt de
  session, choix du badge de démarrage.
- **Répartition de puissance (load balancing)** : budget partagé entre
  bornes basé sur un capteur de puissance réseau externe (positif =
  consommation, négatif = injection), avec :
  - priorité par badge (1-10) + plafond de courant optionnel par badge
  - mode "priorité absolue" par badge (ignore le budget partagé)
  - interrupteur "Boost" manuel par borne (ponctuel, prioritaire sur tout)
  - détection de fin de session (basée sur `stopped_at` réel, pas sur un
    simple creux de puissance transitoire)

## ⚠️ Points d'incertitude à connaître avant de continuer le développement

Plusieurs endpoints de l'API Plugchoice ne sont **pas officiellement
confirmés dans la documentation** — ils ont été déduits par cohérence avec
le reste du schéma OpenAPI, puis certains corrigés après un test réel en
erreur 404. **Un seul (`actions/charge-limit`) a été confirmé par un test
utilisateur réel et fonctionne de manière fiable.** Les autres n'ont pas
été testés en conditions réelles à ce stade :

| Endpoint utilisé | Statut de confirmation |
|---|---|
| `POST /chargers/{id}/actions/charge-limit` | ✅ Confirmé par test réel (corrigé une fois depuis `actions/set-limit`, qui était faux) |
| `POST /chargers/{id}/actions/clear-charge-limit` | ⚠️ Déduit par symétrie avec `actions/charge-limit`, jamais testé. Utilisé par le bouton "Effacer la limite de charge" (désactivé par défaut) et au retrait de l'intégration (`async_remove_entry`). |
| `POST /chargers/{id}/actions/start` | Confirmé par la doc officielle (schéma exact) |
| `POST /chargers/{id}/actions/stop` | ⚠️ Déduit par analogie avec `actions/start`, jamais testé |
| `GET/POST /chargers/{id}/settings/cable-lock` | ⚠️ Déduit par analogie avec `settings/plug-charge`, jamais testé |
| `GET /sites/{id}/cards` | ⚠️ Déduit par analogie de pattern REST, jamais testé |
| `GET /chargers/{id}/logs?filter[method]=SetChargingProfile` | ⚠️ Le nom du paramètre `filter[method]` n'est pas confirmé littéralement (mais un filtrage côté code compense si le filtre serveur ne fonctionne pas) |

**Recommandation** : tester chacun de ces endpoints avec précaution (sur une
borne non critique) avant de s'y fier en production, et corriger dans
`api.py` si une 404 ou un comportement inattendu apparaît (voir l'historique
git pour un exemple de correction de ce type).

## Chute périodique de la limite de charge (identifiée)

Une chute régulière de la limite vers une valeur basse a été observée à
plusieurs reprises. Cause identifiée sur un profil réellement posé :
**Plugchoice fixe la validité de chaque profil `actions/charge-limit` à
`startSchedule + 3 min`** (`validTo` constaté). Comme le régulateur ne
réémettait la limite que sur un changement de cible ≥ 1 A, un profil stable
expirait au bout de 3 min et la borne repassait sans limite jusqu'au cycle
suivant → oscillation.

**Correctif** : le régulateur réémet désormais la même limite toutes les
`LOAD_BALANCING_PROFILE_REFRESH_SECONDS` (120 s) pour maintenir le profil
actif, même si la cible n'a pas bougé.

### Autres observations sur `actions/charge-limit`

- **`stackLevel`** : Plugchoice **écrase à 3** la valeur envoyée
  (`CHARGE_LIMIT_STACK_LEVEL` est transmis mais sans effet). Impossible via
  cet endpoint de passer au-dessus d'un profil de site à un stackLevel ≥ 3.
- **`number_phases`** : le champ **est** accepté et appliqué
  (`numberPhases: 3` dans le profil résultant). On l'envoie explicitement
  pour éviter qu'un profil soit interprété sur une seule phase.
- Le slider « Limite de charge » et le Boost posent aussi un profil à durée
  de vie de 3 min ; **sans load balancing actif, leur limite manuelle
  disparaît au bout de ~3 min** (pas encore de réémission côté entités).

### Causes précédemment écartées (avec confirmation utilisateur)

Plafond de badge, condition de course Boost/régulateur, fausse détection de
fin de session, module « Power Management » natif de Plugchoice, groupe
Plugchoice, limite réelle du circuit, réglage local de la borne — toutes
exclues.

## Autres limitations connues

- Le "Boost" et l'état "borne boostée" sont en mémoire uniquement (non
  persistés) : ils repassent à l'état par défaut au redémarrage de HA.
- Le régulateur déduit le nombre de phases réellement utilisées par le
  véhicule à partir des courants mesurés L1/L2/L3 (un véhicule monophasé
  n'est donc plus bridé comme s'il tirait sur 3 phases). Tant qu'aucun
  courant n'est encore mesuré, il retombe sur le `numberPhases` du dernier
  profil OCPP, puis sur 3 phases / 230 V par défaut.
- Pas de gestion de file d'attente/priorité si le budget ne couvre même
  pas le minimum matériel (6A) pour toutes les bornes actives simultanément
  — chacune reçoit quand même 6A, le budget peut alors être dépassé.
- Erreurs "Erreur API 500" fréquentes observées dans les logs de
  l'utilisateur : elles proviennent de l'API Plugchoice elle-même (upstream),
  pas de l'intégration — le `DataUpdateCoordinator` les gère normalement
  (retry au cycle suivant) mais elles n'ont pas été creusées plus avant.

## Historique de développement

Ce projet a été développé de façon itérative et conversationnelle. Les
détails complets des choix de conception, des tests et des corrections
successives ne sont pas conservés ailleurs que dans l'historique de cette
conversation — ce README et les commentaires du code sont la source de
vérité pour repartir de zéro.
