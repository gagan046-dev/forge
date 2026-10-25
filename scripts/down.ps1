<#
.SYNOPSIS
  Deletes the k3d cluster "forge" and everything in it.

.DESCRIPTION
  Removes the cluster, so also the downloaded models, the Postgres data (API keys,
  usage, conversations, indexed repos) and the sealed-secrets controller key.

  Kept: your plain-text secrets in secrets\*.txt and the sealed files in gitops\envs.
  The next up.ps1 creates a new controller key and re-seals the SAME secrets for it,
  so the admin passwords stay the same.

.PARAMETER Purge
  Also delete the GPU node image (rebuilt by the next up.ps1, which takes a few minutes).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\down.ps1
#>
param([switch]$Purge)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "lib\common.ps1")

Step "Deleting the k3d cluster '$ClusterName'"
if (Quiet "k3d cluster get $ClusterName") {
    Run "k3d cluster delete $ClusterName" "cluster deletion failed"
} else {
    Info "no cluster named '$ClusterName' - nothing to delete"
}

if ($Purge) {
    Step "Removing the node image $NodeImage"
    if (Quiet "docker image inspect $NodeImage") {
        Run "docker image rm $NodeImage" "could not remove $NodeImage"
    } else {
        Info "image not present"
    }
}

Write-Host ""
Write-Host "Done. Secrets in secrets\ were kept; scripts\up.ps1 rebuilds everything."
