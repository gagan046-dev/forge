# common.ps1 - helpers shared by the Forge scripts. Dot-source it:
#   . (Join-Path $PSScriptRoot "lib\common.ps1")
#
# Written for Windows PowerShell 5.1 (the default on Windows 11) and PowerShell 7.

$ClusterName = "forge"
$KubeContext = "k3d-forge"
$NodeImage = "forge/k3s-gpu:v1.35.9-k3s1"
$ServerNode = "k3d-forge-server-0"

function Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Info([string]$Message) { Write-Host "    $Message" }
function Warn([string]$Message) { Write-Host "    WARNING: $Message" -ForegroundColor Yellow }

# Windows PowerShell 5.1 turns anything a native tool writes to stderr into an
# error, and docker/k3d/helm/kubectl print normal progress there. Merging the
# streams inside cmd.exe and checking the exit code avoids false failures.
function Run([string]$Command, [string]$FailMessage) {
    cmd /c "$Command 2>&1" | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "$FailMessage`n  (command: $Command)" }
}

# True when the command exits 0; all output is discarded.
function Quiet([string]$Command) {
    cmd /c "$Command >nul 2>&1"
    return ($LASTEXITCODE -eq 0)
}

# Returns the command's stdout as one trimmed string, or $null when it fails.
# stderr is dropped, so use this only for queries whose failure you handle.
function Capture([string]$Command) {
    $out = cmd /c "$Command 2>nul"
    if ($LASTEXITCODE -ne 0) { return $null }
    return (($out | Out-String).Trim())
}

# Polls $Condition (a script block returning a truthy value) until it succeeds or
# $TimeoutSec passes. Exceptions inside the condition count as "not yet".
function Wait-Until {
    param(
        [Parameter(Mandatory)][string]$Description,
        [Parameter(Mandatory)][scriptblock]$Condition,
        [int]$TimeoutSec = 300,
        [int]$IntervalSec = 5
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $lastNote = Get-Date
    while ($true) {
        $result = $null
        try { $result = & $Condition } catch { $result = $null }
        if ($result) { return $result }
        if ((Get-Date) -ge $deadline) { throw "timed out after $TimeoutSec s waiting for: $Description" }
        if (((Get-Date) - $lastNote).TotalSeconds -ge 30) {
            Info "still waiting for $Description ..."
            $lastNote = Get-Date
        }
        Start-Sleep -Seconds $IntervalSec
    }
}

# Kubernetes objects as PowerShell objects (kubectl -o json | ConvertFrom-Json).
function Get-KubeJson([string]$KubectlArgs) {
    $json = Capture "kubectl --context $KubeContext $KubectlArgs -o json"
    if (-not $json) { return $null }
    return ($json | ConvertFrom-Json)
}

# Names of every Argo CD Application defined in the given manifest files.
function Get-ArgoAppNames([string[]]$Files) {
    $names = @()
    foreach ($file in $Files) {
        $text = [IO.File]::ReadAllText($file)
        foreach ($doc in ($text -split "(?m)^---\s*$")) {
            if ($doc -notmatch "(?m)^kind:\s*Application\s*$") { continue }
            # metadata.name: the "name:" line one level under the top-level "metadata:"
            $inMetadata = $false; $childIndent = $null
            foreach ($line in ($doc -split "\r?\n")) {
                if ($line -match "^metadata:\s*$") { $inMetadata = $true; continue }
                if (-not $inMetadata -or $line -match "^\s*(#.*)?$") { continue }
                if ($line -notmatch "^([ \t]+)(.*)$") { break }          # back at top level
                if ($null -eq $childIndent) { $childIndent = $Matches[1] }
                if ($Matches[1] -eq $childIndent -and $Matches[2] -match "^name:\s*[`"']?([\w.-]+)") {
                    $names += $Matches[1]; break
                }
            }
        }
    }
    return $names
}

# Waits until every named Argo CD Application exists, is Synced and is Healthy.
# Prints the apps that are not there yet every 30 s so a stuck app is obvious.
function Wait-ArgoAppsHealthy([string[]]$Names, [int]$TimeoutSec = 1800) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $lastNote = [datetime]::MinValue
    while ($true) {
        $apps = Get-KubeJson "-n argocd get applications.argoproj.io"
        $pending = @()
        foreach ($name in $Names) {
            $app = $null
            if ($apps) { $app = $apps.items | Where-Object { $_.metadata.name -eq $name } | Select-Object -First 1 }
            if (-not $app) { $pending += "$name (not created yet)"; continue }
            $sync = $app.status.sync.status
            $health = $app.status.health.status
            if ($sync -ne "Synced" -or $health -ne "Healthy") { $pending += "$name ($sync/$health)" }
        }
        if ($pending.Count -eq 0) { return }
        if ((Get-Date) -ge $deadline) {
            throw "Argo CD apps not Healthy after $TimeoutSec s: $($pending -join ', ')`n  (kubectl -n argocd get applications; open http://argocd.localhost:8082)"
        }
        if (((Get-Date) - $lastNote).TotalSeconds -ge 30) {
            Info "waiting: $($pending -join ', ')"
            $lastNote = Get-Date
        }
        Start-Sleep -Seconds 10
    }
}

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal $identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}
