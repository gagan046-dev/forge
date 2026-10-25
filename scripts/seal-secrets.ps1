<#
.SYNOPSIS
  Generates Forge's secrets and seals them for Git (Bitnami Sealed Secrets).

.DESCRIPTION
  1. Makes sure kubeseal is available (downloads a pinned, checksum-verified release
     to C:\Tools\kubeseal\ if it is missing).
  2. Creates random secrets in forge\secrets\*.txt for any that do not exist yet.
     That folder is git-ignored. Existing files are reused, so re-sealing never
     changes a password behind a database's back.
  3. Fetches the public certificate of the cluster's sealed-secrets controller and
     encrypts each Secret with it into gitops\envs\<env>\*.sealed.yaml. Only that
     controller (its private key lives in the cluster) can decrypt them, so the
     sealed files are safe to commit.

  Why re-seal at all? A new cluster ("down.ps1" + "up.ps1") gets a NEW controller
  key, and files sealed for the old key can no longer be decrypted. Re-sealing the
  same plain-text secrets with the new certificate fixes that; commit and push the
  changed *.sealed.yaml files so Argo CD can apply them.

  No secret value is ever printed. The values only travel through a pipe from
  kubectl to kubeseal.

.PARAMETER IfNeeded
  Do nothing when every sealed file exists and was sealed with the controller's
  current certificate. up.ps1 uses this.

.PARAMETER Rotate
  Generate NEW values for every secret. Only do this on a fresh cluster: Postgres
  keeps the password it was initialised with on its volume.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\seal-secrets.ps1
#>
param(
    [switch]$IfNeeded,
    [switch]$Rotate
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "lib\common.ps1")
$root = Split-Path $PSScriptRoot -Parent
$secretsDir = Join-Path $root "secrets"
$certInUse = Join-Path $secretsDir "sealed-secrets-cert.pem"     # cert the current files were sealed with

$KubesealVersion = "0.40.0"
$KubesealSha256 = "e43f3208b41709f69eea03059fcfe5688152543184ee0d7454f0e6d709466821"   # kubeseal-0.40.0-windows-amd64.tar.gz
$KubesealDir = "C:\Tools\kubeseal"

# Which plain-text file goes into which key of which Secret (SPEC section 5).
# The Redis password is one value shared by Redis (forge-inference) and both gateways.
$SealedSecrets = @(
    @{ Namespace = "forge-dev"; Name = "forge-secrets"; Out = "gitops\envs\dev\forge-secrets.sealed.yaml"
       Data = [ordered]@{ "admin-password" = "dev-admin-password"; "session-secret" = "dev-session-secret"; "postgres-password" = "dev-postgres-password" } },
    @{ Namespace = "forge-dev"; Name = "forge-redis"; Out = "gitops\envs\dev\forge-redis.sealed.yaml"
       Data = [ordered]@{ "redis-password" = "redis-password" } },
    @{ Namespace = "forge-prod"; Name = "forge-secrets"; Out = "gitops\envs\prod\forge-secrets.sealed.yaml"
       Data = [ordered]@{ "admin-password" = "prod-admin-password"; "session-secret" = "prod-session-secret"; "postgres-password" = "prod-postgres-password" } },
    @{ Namespace = "forge-prod"; Name = "forge-redis"; Out = "gitops\envs\prod\forge-redis.sealed.yaml"
       Data = [ordered]@{ "redis-password" = "redis-password" } },
    @{ Namespace = "forge-inference"; Name = "forge-redis"; Out = "gitops\envs\inference\forge-redis.sealed.yaml"
       Data = [ordered]@{ "redis-password" = "redis-password" } }
)

# Makes "kubeseal" callable by name (cmd.exe mangles quoted program paths in pipelines).
function Initialize-Kubeseal {
    if (Get-Command kubeseal -ErrorAction SilentlyContinue) { return }
    $exe = Join-Path $KubesealDir "kubeseal.exe"
    if (Test-Path $exe) { $env:PATH = "$KubesealDir;$env:PATH"; return }

    Step "Downloading kubeseal v$KubesealVersion to $KubesealDir"
    New-Item -ItemType Directory -Force $KubesealDir | Out-Null
    $archive = Join-Path $env:TEMP "kubeseal-$KubesealVersion-windows-amd64.tar.gz"
    $url = "https://github.com/bitnami-labs/sealed-secrets/releases/download/v$KubesealVersion/kubeseal-$KubesealVersion-windows-amd64.tar.gz"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing
    $hash = (Get-FileHash -Algorithm SHA256 $archive).Hash.ToLower()
    if ($hash -ne $KubesealSha256) {
        Remove-Item $archive -Force
        throw "kubeseal download checksum mismatch (got $hash) - refusing to use it"
    }
    # tar.exe ships with Windows 10 1803+ and Windows 11.
    Run "tar -xzf `"$archive`" -C `"$KubesealDir`" kubeseal.exe" "could not unpack kubeseal"
    Remove-Item $archive -Force
    $env:PATH = "$KubesealDir;$env:PATH"
}

# 32 bytes from the OS CSPRNG, URL-safe base64 (no + / =), so it is safe in URLs and YAML.
function New-RandomSecret([int]$Bytes = 32) {
    $buffer = New-Object byte[] $Bytes
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return ([Convert]::ToBase64String($buffer)).Replace("+", "-").Replace("/", "_").TrimEnd("=")
}

function Get-SecretFile([string]$Name) { return (Join-Path $secretsDir "$Name.txt") }

# The controller's Service: the Helm chart names it after the release, so look it up
# by label instead of assuming kubeseal's default "sealed-secrets-controller".
function Get-Controller {
    $services = Get-KubeJson "get service -A -l app.kubernetes.io/name=sealed-secrets"
    $svc = $services.items | Where-Object { $_.metadata.name -notmatch "metrics" } | Select-Object -First 1
    if (-not $svc) { throw "no sealed-secrets controller Service found (is the sealed-secrets Argo CD app Healthy?)" }
    return @{ Namespace = $svc.metadata.namespace; Name = $svc.metadata.name }
}

Initialize-Kubeseal
$controller = Get-Controller
New-Item -ItemType Directory -Force $secretsDir | Out-Null

Step "Fetching the sealing certificate from $($controller.Namespace)/$($controller.Name)"
$certNow = Join-Path $secretsDir "sealed-secrets-cert.current.pem"
# Not via Run: its "2>&1" would mix error text into the certificate file.
cmd /c "kubeseal --context $KubeContext --controller-namespace $($controller.Namespace) --controller-name $($controller.Name) --fetch-cert > `"$certNow`""
if ($LASTEXITCODE -ne 0 -or -not (Select-String -Path $certNow -Pattern "BEGIN CERTIFICATE" -Quiet)) {
    throw "could not fetch the controller certificate"
}

if ($IfNeeded -and -not $Rotate) {
    $allSealed = -not ($SealedSecrets | Where-Object { -not (Test-Path (Join-Path $root $_.Out)) })
    $sameKey = (Test-Path $certInUse) -and
        ((Get-FileHash $certInUse).Hash -eq (Get-FileHash $certNow).Hash)
    if ($allSealed -and $sameKey) {
        Remove-Item $certNow -Force
        Info "sealed secrets are up to date for this cluster's key - nothing to do"
        return
    }
    if (-not $allSealed) { Info "some sealed files are missing - sealing" }
    else { Info "the controller key changed (new cluster?) - re-sealing the same secrets with the new key" }
}

Step "Preparing plain-text secrets in $secretsDir (git-ignored, never printed)"
$secretNames = $SealedSecrets | ForEach-Object { $_.Data.Values } | Sort-Object -Unique
foreach ($name in $secretNames) {
    $file = Get-SecretFile $name
    if ((Test-Path $file) -and -not $Rotate) { Info "$name.txt exists - reusing it"; continue }
    # No trailing newline: kubectl --from-file takes the file's bytes verbatim.
    [IO.File]::WriteAllText($file, (New-RandomSecret), (New-Object Text.UTF8Encoding $false))
    Info "$name.txt generated"
}

Step "Sealing"
foreach ($s in $SealedSecrets) {
    $out = Join-Path $root $s.Out
    New-Item -ItemType Directory -Force (Split-Path $out -Parent) | Out-Null
    $fromFiles = ($s.Data.GetEnumerator() | ForEach-Object { "--from-file=$($_.Key)=`"$(Get-SecretFile $_.Value)`"" }) -join " "
    $errFile = [IO.Path]::GetTempFileName()
    # Both tools run inside one cmd.exe pipeline: the Secret YAML (with the values)
    # goes straight from kubectl into kubeseal and never reaches PowerShell or a file.
    cmd /c "kubectl create secret generic $($s.Name) -n $($s.Namespace) $fromFiles --dry-run=client -o yaml | kubeseal --cert `"$certNow`" --format yaml > `"$out`" 2> `"$errFile`""
    $code = $LASTEXITCODE
    $err = (Get-Content $errFile -Raw -ErrorAction SilentlyContinue); Remove-Item $errFile -Force
    if ($code -ne 0 -or -not (Select-String -Path $out -Pattern "kind: SealedSecret" -Quiet)) {
        throw "sealing $($s.Namespace)/$($s.Name) failed: $err"
    }
    Info "$($s.Out)  ($($s.Namespace)/$($s.Name))"
}
Move-Item -Force $certNow $certInUse

Write-Host ""
Write-Host "Sealed. Commit and push the changed files so Argo CD can apply them:"
Write-Host "  git add gitops/envs; git commit -m `"chore: re-seal secrets`"; git push"
Write-Host "Admin passwords: secrets\dev-admin-password.txt and secrets\prod-admin-password.txt"
