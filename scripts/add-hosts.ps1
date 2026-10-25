<#
.SYNOPSIS
  Maps the Forge host names to 127.0.0.1 in the Windows hosts file (needs admin).

.DESCRIPTION
  Browsers resolve *.localhost to 127.0.0.1 on their own, but most other programs
  (Python, curl, Node.js and therefore VS Code extensions such as Continue) ask
  Windows, and Windows does not. Without these entries the OpenAI SDK cannot reach
  http://forge.localhost:8082/v1. Entries that already exist are left alone.
  up.ps1 starts this script elevated when a name does not resolve.

.EXAMPLE
  # from an Administrator PowerShell
  powershell -ExecutionPolicy Bypass -File scripts\add-hosts.ps1
#>
param([string]$HostsFile = (Join-Path $env:SystemRoot "System32\drivers\etc\hosts"))
$ErrorActionPreference = "Stop"
$hostNames = "forge.localhost", "forge-dev.localhost", "grafana.localhost", "argocd.localhost"

$current = [IO.File]::ReadAllText($HostsFile)
$missing = $hostNames | Where-Object { $current -notmatch "(?m)^\s*127\.0\.0\.1\s+([^#\r\n]*\s)?$([regex]::Escape($_))(\s|$)" }
if (-not $missing) {
    Write-Host "hosts file already maps: $($hostNames -join ', ')"
    exit 0
}

$block = "`r`n# Forge (k3d ingress on localhost:8082)`r`n" + (($missing | ForEach-Object { "127.0.0.1 $_" }) -join "`r`n") + "`r`n"
if (-not $current.EndsWith("`n")) { $block = "`r`n" + $block }
[IO.File]::AppendAllText($HostsFile, $block, [Text.Encoding]::ASCII)
Write-Host "added to ${hostsFile}: $($missing -join ', ')"
