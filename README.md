# DealBot — alertes Dealabs → Discord

Surveille les flux RSS de Dealabs et poste dans un salon Discord dès qu'un composant
passe **sous ton prix plafond** : cartes graphiques, processeurs, RAM, SSD, disques NAS,
cartes mères, alimentations, refroidissement, boîtiers, écrans, périphériques, réseau et homelab.

Deux types d'alertes :

- **Cibles** : un composant précis sous un prix plafond (41 cibles préconfigurées dans `config.yaml`).
- **Pépites 🔥** : n'importe quel deal informatique qui devient « hot » sur Dealabs, même hors cibles.

Le bot **te prévient lui-même sur Discord** quand il faut intervenir : si Dealabs devient
illisible ou si le bot plante 3 fois d'affilée (~45 min), tu reçois « ⚠️ DealBot a besoin de toi »
avec la cause et la marche à suivre (au plus une fois par jour), puis « ✅ fonctionne de nouveau »
quand ça repart. Le workflow se maintient aussi actif tout seul (pas de pause après 60 jours).

Anti-doublon intégré : chaque deal n'est notifié qu'une fois. Au premier lancement, le bot
mémorise les deals déjà en ligne **sans les envoyer**, pour ne pas inonder le salon.

```
dealbot/
├── bot.py                        # le bot (Python 3.10+, stdlib + PyYAML)
├── config.yaml                   # tes cibles et prix plafonds
├── requirements.txt
├── .github/workflows/dealbot.yml # exécution toutes les 15 min sur GitHub Actions
├── setup.ps1                     # installation automatique (Windows)
├── Dockerfile / docker-compose.yml  # option : hébergement sur homelab / VPS
└── .gitignore
```

---

## Installation express (Windows)

1. Crée le webhook Discord (étape 1 ci-dessous) et copie son URL.
2. Dans le dossier du projet, lance :

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

Le script installe les outils manquants (Git, GitHub CLI, Python via winget), te connecte à
GitHub, teste le webhook, crée le dépôt public, enregistre le secret et lance le premier run.
Option : `-RepoName autre-nom` ou `-Private`.

## Mise en place manuelle (GitHub Actions, gratuit)

### 1. Créer le webhook Discord

1. Crée un serveur Discord perso (bouton **+** → *Créer le mien*) et un salon `#bons-plans`.
2. Clic droit sur le salon → **Modifier le salon** → **Intégrations** → **Webhooks** → **Nouveau webhook**.
3. Renomme-le (ex. *DealBot*), puis **Copier l'URL du webhook**.

> ⚠️ Cette URL est un secret : toute personne qui l'a peut poster dans ton salon.
> Ne la mets jamais dans le code ni dans un commit.

### 2. Créer le dépôt GitHub

Dépôt **public** = minutes GitHub Actions illimitées (l'URL du webhook reste dans les secrets).

```bash
cd dealbot
git init -b main
git add .
git commit -m "DealBot : alertes Dealabs vers Discord"
gh repo create dealbot --public --source=. --push   # ou crée le dépôt sur github.com puis git remote add + git push
```

### 3. Ajouter le secret

Sur GitHub : **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Name | Secret |
|---|---|
| `DISCORD_WEBHOOK_URL` | l'URL copiée à l'étape 1 |

Ou en ligne de commande : `gh secret set DISCORD_WEBHOOK_URL`

### 4. Lancer et vérifier

1. Onglet **Actions** → workflow **DealBot** → **Run workflow**.
2. Ouvre le run → étape *Vérifier les deals*. Tu dois voir :
   - `flux OK : https://www.dealabs.com/rss/new` (et `/rss/hot`)
   - `XX deals analysés`
   - `premier lancement : N deals existants mémorisés sans notification`
3. Ensuite, le bot tourne tout seul toutes les 15 minutes.

Tester l'envoi Discord depuis ton PC :

```powershell
# PowerShell
$env:DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."
pip install -r requirements.txt
python bot.py --test-webhook
```

---

## Personnaliser les cibles

Tout se passe dans `config.yaml`. Exemple :

```yaml
  - name: AMD Radeon RX 9070
    category: gpu            # gpu cpu ram ssd hdd mobo psu cooling case screen periph network homelab
    max_price: 650           # alerte si prix <= 650 €
    min_price: 300           # ignore les accessoires (backplate, support GPU...)
    include: ['\brx ?9070\b(?! ?(xt|gre)\b)']   # au moins un motif doit matcher
    require: []              # TOUS ces motifs doivent matcher (ex : capacité 16 Go)
    exclude: []              # un seul suffit à ignorer le deal
```

Les motifs sont des regex appliquées au titre **normalisé** : minuscules, sans accents,
tirets remplacés par des espaces (`Écran RX-9070` → `ecran rx 9070`).

Tester une modif sans rien envoyer :

```bash
python bot.py --dry-run -v
```

Options utiles dans `settings` :

- `mention: "@here"` pour une notification forte sur ton téléphone ;
- `notify_on_first_run: true` pour recevoir aussi les deals déjà en ligne au premier lancement.

---

## Option : héberger sur ton homelab ou un VPS

Même script, vérification toutes les 10 minutes, en continu :

```bash
echo 'DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...' > .env
chmod 600 .env
docker compose up -d --build
docker compose logs -f
```

`config.yaml` est monté en lecture seule : modifie-le puis `docker compose restart`.
L'état anti-doublon est conservé dans le volume `dealbot-data`.

---

## Dépannage

| Symptôme | Cause / solution |
|---|---|
| `flux ignoré ... HTTP Error 403` | Dealabs bloque l'IP des runners GitHub. Passe sur l'hébergement Docker (homelab ou VPS). |
| Le workflow ne tourne plus | Normalement évité par l'étape *Keepalive*. Sinon : onglet Actions → *Enable workflow*. |
| Retard de quelques minutes | Normal : le cron GitHub n'est pas précis aux heures chargées. |
| Une alerte manque | Le prix doit être lisible (attribut marchand du flux, ou `xx,xx €` dans le titre). Lance `python bot.py --dry-run -v` pour voir les deals ignorés. |
| Fausse alerte | Ajoute un motif dans `exclude` de la cible, ou dans `global_exclude`. |

Le bot ne lit que les flux RSS publics de Dealabs (2 requêtes toutes les 15 min) : aucune
extraction de pages, aucune connexion à un compte.
