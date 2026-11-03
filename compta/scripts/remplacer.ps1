# Trip Compta (Windows) : remplace l'application par la nouvelle version, la relance, et vérifie qu'elle répond.
# Si elle ne répond pas, l'ancienne version est remise en place et relancée.
# Lancé par l'application elle-même, juste avant qu'elle se ferme. Les données ne sont jamais touchées.
#
#   remplacer.ps1 PID CIBLE NOUVELLE PROGRAMME PORT VERSION TRAVAIL [options de lancement…]
param(
  [Parameter(Mandatory = $true)][int]$AppPid,
  [Parameter(Mandatory = $true)][string]$Target,
  [Parameter(Mandatory = $true)][string]$New,
  [Parameter(Mandatory = $true)][string]$ExeRel,
  [Parameter(Mandatory = $true)][int]$Port,
  [Parameter(Mandatory = $true)][string]$Version,
  [Parameter(Mandatory = $true)][string]$Work,
  [Parameter(ValueFromRemainingArguments = $true)][string[]]$AppArgs
)
$ErrorActionPreference = "Continue"
$Old = "$Target.ancienne"
$Log = Join-Path $Work "maj.log"
$WaitPing = 90
if ($env:TRIPCOMPTA_MAJ_ATTENTE) { $WaitPing = [int]$env:TRIPCOMPTA_MAJ_ATTENTE }

function Log($m) { Add-Content -Path $Log -Value ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m) -Encoding UTF8 }
function Fail($m) { Log "ÉCHEC : $m"; Set-Content -Path (Join-Path $Work "echec.txt") -Value $m -Encoding UTF8 }

function Launch($app) {
  $exe = Join-Path $app $ExeRel
  $argList = @("--no-browser", "--port", "$Port")
  if ($AppArgs) { $argList += $AppArgs }
  $quoted = $argList | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }
  $env:TRIPCOMPTA_RESTARTED = "1"
  if ($exe -match '\.(cmd|bat)$') {
    # cmd /c "  "programme" options  " : les guillemets extérieurs protègent ceux de l'intérieur
    $line = '/c "' + '"' + $exe + '" ' + ($quoted -join ' ') + '"'
    Start-Process -FilePath "cmd.exe" -ArgumentList $line -WindowStyle Hidden
  } else {
    Start-Process -FilePath $exe -ArgumentList $quoted -WorkingDirectory $app
  }
}

# Requêtes directes, sans proxy : Invoke-RestMethod cherche un proxy à chaque appel (très lent sur certains PC)
function Http($method, $path) {
  try {
    $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port$path")
    $req.Method = $method
    $req.Proxy = $null
    $req.Timeout = 2000
    $req.ReadWriteTimeout = 2000
    if ($method -eq "POST") { $req.ContentLength = 0 }
    $resp = $req.GetResponse()
    $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
    $body = $reader.ReadToEnd()
    $reader.Close(); $resp.Close()
    return $body
  } catch { return "" }
}

function Ping-Version {
  $body = Http "GET" "/ping"
  if ($body -match '"version"\s*:\s*"([^"]*)"') { return $Matches[1] }
  return ""
}

function Move-Folder($from, $to) {
  try { Move-Item -LiteralPath $from -Destination $to -ErrorAction Stop; return $true }
  catch {
    try {                     # autre disque : copier puis effacer
      Copy-Item -LiteralPath $from -Destination $to -Recurse -ErrorAction Stop
      Remove-Item -LiteralPath $from -Recurse -Force -ErrorAction SilentlyContinue
      return $true
    } catch { return $false }
  }
}

Log "Mise à jour vers $Version : attente de la fermeture de l'application ($AppPid)"
$p = Get-Process -Id $AppPid -ErrorAction SilentlyContinue
if ($p) {
  if (-not $p.WaitForExit(60000)) { Log "L'application ne se ferme pas : arrêt forcé"; Stop-Process -Id $AppPid -Force -ErrorAction SilentlyContinue }
}
Start-Sleep -Milliseconds 800   # Windows libère les fichiers un peu après la fin du programme

if (Test-Path -LiteralPath $Old) { Remove-Item -LiteralPath $Old -Recurse -Force -ErrorAction SilentlyContinue }
$moved = $false
for ($i = 0; $i -lt 20 -and -not $moved; $i++) {
  try { Move-Item -LiteralPath $Target -Destination $Old -ErrorAction Stop; $moved = $true }
  catch { Start-Sleep -Milliseconds 500 }
}
if (-not $moved) {
  Fail "Impossible de mettre l'ancienne version de côté. Rien n'a été changé."
  Launch $Target
  exit 1
}
if (-not (Move-Folder $New $Target)) {
  if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Recurse -Force -ErrorAction SilentlyContinue }
  Move-Item -LiteralPath $Old -Destination $Target
  Fail "Impossible d'installer la nouvelle version. L'ancienne a été remise."
  Launch $Target
  exit 1
}
Get-ChildItem -LiteralPath $Target -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
Log "Nouvelle version en place : lancement"
Launch $Target

for ($i = 0; $i -lt $WaitPing; $i++) {
  if ((Ping-Version) -eq $Version) {
    Remove-Item -LiteralPath $Old -Recurse -Force -ErrorAction SilentlyContinue
    Set-Content -Path (Join-Path $Work "reussi.txt") -Value $Version -Encoding UTF8
    Remove-Item -LiteralPath (Join-Path $Work "nouvelle") -Recurse -Force -ErrorAction SilentlyContinue
    Log "Terminé : la version $Version répond"
    exit 0
  }
  Start-Sleep -Seconds 1
}

Log "La nouvelle version ne répond pas : retour à l'ancienne"
Http "POST" "/quitter" | Out-Null
Start-Sleep -Seconds 1
Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 800
Remove-Item -LiteralPath $Target -Recurse -Force -ErrorAction SilentlyContinue
Move-Item -LiteralPath $Old -Destination $Target
Fail "La version $Version n'a pas démarré correctement. L'ancienne version a été remise en place."
Launch $Target
exit 1
