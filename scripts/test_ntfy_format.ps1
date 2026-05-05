<#
.SYNOPSIS
  Teste les 4 formats de notification ntfy utilises par Archive.yml.

.DESCRIPTION
  Envoie 4 notifications de test au topic ntfy specifie, reproduisant exactement
  les en-tetes (Title/Priority/Tags) et les bodies utilises par le step
  "Notification ntfy" dans .github/workflows/Archive.yml. Permet de valider
  visuellement sur le telephone que les accents, le tiret long, les emojis
  et les priorites s'affichent correctement.

  Doit rester en miroir d'Archive.yml : si tu modifies les titres/bodies
  dans le workflow, mets ce script a jour dans le meme commit.

.PARAMETER NtfyUrl
  URL complete du topic ntfy (ex: https://ntfy.sh/test-archive-XXXXX).
  Conseil : utiliser un topic de TEST distinct du topic prod pour ne pas
  polluer l'historique des vraies notifs d'archivage.

.EXAMPLE
  .\test_ntfy_format.ps1 -NtfyUrl "https://ntfy.sh/test-archive-7f3k2"

.NOTES
  Pre-requis : Windows 10+ (curl.exe bundle) + PowerShell 5.1 ou superieur.
  Aucune dependance externe.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0,
               HelpMessage = "URL complete du topic ntfy a tester")]
    [ValidatePattern('^https?://')]
    [string]$NtfyUrl
)

# Forcer UTF-8 sur la sortie console et l'encodage des arguments passes a curl.exe
# (sinon Windows convertit en CP1252 et casse le tiret long « — »).
$OutputEncoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

# Les 4 scenarios. Doit rester aligne avec Archive.yml step "Notification ntfy (succes)"
# et "Notification ntfy (echec)".
$scenarios = @(
    @{
        Title = "Archivage Proton OK"
        Prio  = "2"
        Tags  = "white_check_mark,floppy_disk"
        Body  = "Date : 2026-05-05`nZones : 11`nImages : 1562`nTaille ZIP : 532M`nVérifié : true`nTrigger : schedule`nAction : nouveau fichier uploadé"
    },
    @{
        Title = "Archivage Proton — fichier remplacé"
        Prio  = "2"
        Tags  = "warning,floppy_disk"
        Body  = "Date : 2026-05-05`nZones : 11`nImages : 1562`nTaille ZIP : 532M`nVérifié : true`nTrigger : schedule`nAction : remplacé (ancien fichier 340123456 bytes, incomplet ou différent)"
    },
    @{
        Title = "Archivage Proton — déjà à jour"
        Prio  = "1"
        Tags  = "information_source"
        Body  = "Date : 2026-05-04`nZones : 11`nImages : 1562`nTaille ZIP : 532M`nVérifié : true`nTrigger : schedule`nAction : skip (fichier identique déjà présent)"
    },
    @{
        Title = "Archivage Proton — ÉCHEC"
        Prio  = "4"
        Tags  = "rotating_light"
        Body  = "Date : 2026-05-05`nVérifié : false`nAction tentée : uploaded`nTrigger : schedule`nRun : 67531869493`n`nVoir : https://github.com/ox30/tomtom_monitoring_a13-a2/actions/runs/67531869493`n`nRattrapage manuel : Actions > Archive to Proton Drive > Run workflow > date=2026-05-05"
    }
)

Write-Host ""
Write-Host "Envoi de $($scenarios.Count) notifications de test vers :" -ForegroundColor Yellow
Write-Host "  $NtfyUrl" -ForegroundColor Yellow
Write-Host ""

foreach ($s in $scenarios) {
    Write-Host "  -> $($s.Title)  (P$($s.Prio))" -ForegroundColor Cyan
    curl.exe -s `
        -H "Title: $($s.Title)" `
        -H "Priority: $($s.Prio)" `
        -H "Tags: $($s.Tags)" `
        -d $s.Body `
        $NtfyUrl | Out-Null

    if ($LASTEXITCODE -ne 0) {
        Write-Host "     ATTENTION : curl a renvoye le code de sortie $LASTEXITCODE" -ForegroundColor Red
    }
    Start-Sleep -Seconds 2  # delai pour eviter le groupement de notifs cote ntfy
}

Write-Host ""
Write-Host "Terminé. Verifie ton telephone — tu devrais voir 4 notifs :" -ForegroundColor Green
Write-Host "  - P4 (echec)         -> vibration insistante"
Write-Host "  - P2 (OK / remplace) -> silencieux mais visible"
Write-Host "  - P1 (deja a jour)   -> discret, pas de pop-up"
Write-Host ""
Write-Host "Verifications visuelles a faire :"
Write-Host "  [ ] Accents corrects (e, a, E)"
Write-Host "  [ ] Tiret long « — » correct (et pas un « ? »)"
Write-Host "  [ ] Emojis presents : OK, sauvegarde, attention, info, alerte"
