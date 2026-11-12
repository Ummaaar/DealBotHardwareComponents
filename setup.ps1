<#
.SYNOPSIS
    Installe DealBot en une commande : teste le webhook Discord, crée le dépôt GitHub,
    ajoute le secret et lance le premier run.

.DESCRIPTION
    Prérequis : Git, Python et GitHub CLI (gh). Le script propose de les installer via winget.
    Seule étape manuelle : créer le webhook dans Discord (voir README) et copier son URL.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup.ps1
    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -RepoName mon-dealbot -Private
#>
[CmdletBinding()]
param(
    [string]$RepoName = "dealbot",
    [switch]$Private
)

# Les commandes natives (git, gh) sont vérifiées via $LASTEXITCODE
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

function Write-Step([string]$msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "    [OK] $msg" -ForegroundColor Green }
function Fail([string]$msg)       { Write-Host "    [ERREUR] $msg" -ForegroundColor Red; exit 1 }

function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Ensure-Tool([string]$cmd, [string]$wingetId, [string]$label) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { Write-Ok "$label présent"; return }
    Write-Host "    $label introuvable." -ForegroundColor Yellow
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail "Installe $label manuellement puis relance le script."
    }
    $answer = Read-Host "    L'installer avec winget ($wingetId) ? [O/n]"
    if ($answer -match '^[nN]') { Fail "$label est requis." }
    winget install --id $wingetId -e --source winget --accept-package-agreements --accept-source-agreements
    Update-SessionPath
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Fail "$label installé mais pas encore dans le PATH : ferme et rouvre PowerShell, puis relance le script."
    }
    Write-Ok "$label installé"
}

# ---------------------------------------------------------------------------
Write-Step "Vérification des outils"
Ensure-Tool git    "Git.Git"           "Git"
Ensure-Tool gh     "GitHub.cli"        "GitHub CLI"
$python = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } elseif (Get-Command python -ErrorAction SilentlyContinue) { "python" } else { $null }
if (-not $python) { Ensure-Tool python "Python.Python.3.12" "Python"; $python = "python" } else { Write-Ok "Python présent" }

# ---------------------------------------------------------------------------
Write-Step "Connexion à GitHub"
gh auth status *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "    Une page va s'ouvrir dans ton navigateur pour autoriser GitHub CLI."
    gh auth login --hostname github.com --git-protocol https --web
    if ($LASTEXITCODE -ne 0) { Fail "Connexion GitHub échouée." }
}
gh auth setup-git | Out-Null   # git push utilisera la connexion gh
$ghUser = (gh api user --jq .login).Trim()
Write-Ok "Connecté en tant que $ghUser"

# ---------------------------------------------------------------------------
Write-Step "Webhook Discord"
Write-Host "    Discord : clic droit sur ton salon > Modifier le salon > Intégrations > Webhooks"
Write-Host "    > Nouveau webhook > Copier l'URL du webhook."
$secure = Read-Host "    Colle l'URL du webhook (elle ne s'affiche pas)" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try   { $webhook = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim() }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }

if ($webhook -notmatch '^https://(ptb\.|canary\.)?(discord|discordapp)\.com/api/webhooks/\d+/[\w-]+$') {
    Fail "Ce n'est pas une URL de webhook Discord valide (https://discord.com/api/webhooks/...)."
}

$body = @{ username = "DealBot"; content = "✅ DealBot : webhook connecté, installation en cours..." } | ConvertTo-Json
try {
    Invoke-RestMethod -Uri $webhook -Method Post -ContentType "application/json; charset=utf-8" `
        -Body ([Text.Encoding]::UTF8.GetBytes($body)) -ErrorAction Stop | Out-Null
    Write-Ok "Message de test envoyé : vérifie ton salon Discord"
} catch {
    Fail "Discord a refusé le webhook ($($_.Exception.Message)). Vérifie l'URL."
}

# ---------------------------------------------------------------------------
Write-Step "Test local du bot (sans envoi)"
& $python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "Installation de PyYAML échouée." }
& $python -c "import yaml; yaml.safe_load(open('config.yaml', encoding='utf-8')); print('    [OK] config.yaml valide')"
if ($LASTEXITCODE -ne 0) { Fail "config.yaml invalide." }

# ---------------------------------------------------------------------------
Write-Step "Dépôt Git local"
if (-not (Test-Path .git)) {
    git init -b main | Out-Null
    Write-Ok "Dépôt initialisé"
}
# Projet cloné depuis le dépôt d'origine : on garde ce lien sous le nom "upstream"
# pour que "origin" pointe vers TON propre dépôt.
$origin = git remote get-url origin 2>$null
if ($origin -and $origin -notmatch "github\.com[/:]$ghUser/") {
    git remote rename origin upstream
    Write-Ok "Dépôt d'origine conservé comme 'upstream' (pour récupérer les mises à jour)"
}
if (-not (git config user.name))  { git config user.name  $ghUser }
if (-not (git config user.email)) { git config user.email "$ghUser@users.noreply.github.com" }

git add -A
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "DealBot : alertes Dealabs vers Discord" | Out-Null
    Write-Ok "Commit créé"
} else {
    Write-Ok "Rien de nouveau à commiter"
}

# ---------------------------------------------------------------------------
Write-Step "Dépôt GitHub $ghUser/$RepoName"
gh repo view "$ghUser/$RepoName" *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "    Le dépôt existe déjà, envoi des modifications." -ForegroundColor Yellow
    if (-not (git remote 2>$null | Select-String -Quiet '^origin$')) {
        git remote add origin "https://github.com/$ghUser/$RepoName.git"
    }
    git push -u origin main
} else {
    $visibility = if ($Private) { "--private" } else { "--public" }
    gh repo create $RepoName $visibility --source . --remote origin --push `
        --description "Alertes Dealabs vers Discord quand un composant PC passe sous mon prix plafond"
}
if ($LASTEXITCODE -ne 0) { Fail "Création ou push du dépôt échoué." }
Write-Ok "Code en ligne : https://github.com/$ghUser/$RepoName"
if ($Private) {
    Write-Host "    Dépôt privé : environ 2 000 min/mois d'Actions, ça reste suffisant toutes les 15 min mais c'est juste." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
Write-Step "Secret DISCORD_WEBHOOK_URL"
$webhook | gh secret set DISCORD_WEBHOOK_URL --repo "$ghUser/$RepoName"
if ($LASTEXITCODE -ne 0) { Fail "Ajout du secret échoué." }
Remove-Variable webhook
Write-Ok "Secret enregistré dans GitHub (jamais dans le code)"

# ---------------------------------------------------------------------------
Write-Step "Premier lancement"
foreach ($try in 1..3) {   # GitHub met quelques secondes à détecter un nouveau workflow
    Start-Sleep -Seconds 10
    gh workflow run dealbot.yml --repo "$ghUser/$RepoName" 2>$null
    if ($LASTEXITCODE -eq 0) { break }
}
if ($LASTEXITCODE -ne 0) {
    Write-Host "    Lancement auto impossible : va dans l'onglet Actions > DealBot > Run workflow." -ForegroundColor Yellow
} else {
    Write-Ok "Workflow lancé"
    Start-Sleep -Seconds 8
    $runId = (gh run list --repo "$ghUser/$RepoName" --workflow dealbot.yml --limit 1 --json databaseId --jq '.[0].databaseId')
    if ($runId) {
        Write-Host "    Suivi du run (environ 1 min)..."
        gh run watch $runId --repo "$ghUser/$RepoName" --exit-status
        Write-Host "`n    Extrait des logs :" -ForegroundColor Cyan
        gh run view $runId --repo "$ghUser/$RepoName" --log |
            Select-String -Pattern 'flux OK|flux ignoré|deals analysés|premier lancement|ALERTE|aucune nouvelle|ERROR' |
            ForEach-Object { "    " + ($_.Line -split "`t")[-1] }
    }
}

# ---------------------------------------------------------------------------
Write-Step "Dernière étape (recommandée) : déclenchement fiable avec cron-job.org"
Write-Host "    La planification intégrée de GitHub n'est pas garantie (elle peut ne jamais démarrer"
Write-Host "    sur un dépôt neuf). cron-job.org va lancer le bot toutes les 15 min, gratuitement."
Write-Host ""
Write-Host "    1) Crée un jeton limité à ce dépôt (la page va s'ouvrir) :" -ForegroundColor Cyan
Write-Host "       - Expiration : 1 an"
Write-Host "       - Repository access : Only select repositories > $RepoName"
Write-Host "       - Permissions > Repository permissions > Actions : Read and write"
Write-Host "       - Generate token, puis copie-le (github_pat_...)"
Write-Host ""
Write-Host "    2) Sur https://cron-job.org > Create cronjob :" -ForegroundColor Cyan
Write-Host "       URL        : https://api.github.com/repos/$ghUser/$RepoName/actions/workflows/dealbot.yml/dispatches"
Write-Host "       Fréquence  : toutes les 15 minutes"
Write-Host "       Avancé > Méthode : POST"
Write-Host "       Avancé > En-têtes :"
Write-Host "         Authorization = Bearer <ton jeton>"
Write-Host "         Accept        = application/vnd.github+json"
Write-Host "         User-Agent    = cron-dealbot"
Write-Host "       Avancé > Corps : {""ref"":""main""}"
Write-Host ""
Write-Host "    3) Test run : la réponse doit être 204, puis Créer." -ForegroundColor Cyan
$open = Read-Host "`n    Ouvrir la page de création du jeton maintenant ? [O/n]"
if ($open -notmatch '^[nN]') {
    Start-Process "https://github.com/settings/personal-access-tokens/new"
    Start-Process "https://cron-job.org/en/members/jobs/add/"
}

Write-Host "`n============================================================" -ForegroundColor Green
Write-Host " DealBot est en place. Il vérifie Dealabs toutes les 15 minutes." -ForegroundColor Green
Write-Host " Tes cibles et prix plafonds : config.yaml (git commit + git push après modif)" -ForegroundColor Green
Write-Host " Suivi : https://github.com/$ghUser/$RepoName/actions" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host " Si tu vois 'flux ignoré ... 403' ci-dessus, Dealabs bloque GitHub :" -ForegroundColor Yellow
Write-Host " passe sur l'hébergement Docker (voir README)." -ForegroundColor Yellow
