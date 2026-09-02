# Ressources de marque (icône / logo)

> ⚠️ **Home Assistant et HACS ne lisent PAS ce dossier**, ni un dossier
> `brand/` placé dans `custom_components/plugchoice/`. Les icônes des
> intégrations sont servies depuis le dépôt central
> [`home-assistant/brands`](https://github.com/home-assistant/brands).
> Tant que le domaine `plugchoice` n'y est pas ajouté, aucune icône ne
> s'affiche (c'est aussi le check « brands » ignoré dans le workflow HACS).

## Contenu

`custom_integrations/plugchoice/` reproduit exactement l'arborescence
attendue par `home-assistant/brands` :

| Fichier | Taille | Usage |
|---|---|---|
| `icon.png` | 256×256 | icône (thème clair) |
| `icon@2x.png` | 512×512 | icône HiDPI |
| `logo.png` | h. 256 | logo (thème clair) |
| `logo@2x.png` | h. 512 | logo HiDPI |
| `dark_icon.png` / `dark_icon@2x.png` | idem | variante thème sombre |
| `dark_logo.png` / `dark_logo@2x.png` | idem | variante thème sombre |

Toutes sont sur fond **transparent** et détourées. `source/icon-original.png`
est le fichier fourni au départ (fond crème opaque, 236×256), conservé pour
référence.

## Soumettre à home-assistant/brands

1. Fork de `home-assistant/brands`
2. Copier le dossier `custom_integrations/plugchoice/` de ce dépôt à la
   racine `custom_integrations/` du fork
3. `python3 -m script.validate` (ou laisser la CI du dépôt le faire)
4. Ouvrir la PR ; une fois mergée, l'icône apparaît dans HA et HACS

Détails et contraintes : <https://github.com/home-assistant/brands#adding-a-new-brand>
