<#
.SYNOPSIS
  Brings up the whole Forge platform on this laptop, from zero. Safe to re-run.

.DESCRIPTION
  1. Checks prerequisites (Docker Desktop running, k3d, kubectl, helm, git, NVIDIA
     driver) and maps the *.localhost names in the hosts file.
  2. Builds the GPU node image forge/k3s-gpu:v1.35.9-k3s1 (cluster/Dockerfile).
  3. Creates the k3d cluster "forge" (or starts the existing one).
  4. Installs the node DNS fix and the NVIDIA device plugin.
  5. Waits until the node advertises nvidia.com/gpu.
  6. Installs Argo CD with Helm and applies the root "app of apps"; Argo CD then
     deploys everything else from https://github.com/gagan046-dev/forge (main).
  7. Waits for the sealed-secrets controller, then seals the secrets if the sealed
     files are missing or were sealed for another cluster's key (see seal-secrets.ps1).
  8. Waits until every Argo CD application is Synced and Healthy.
  9. Prints the URLs and where the admin password is stored.

  Argo CD deploys what is on GitHub, not what is on disk: push your commits (and
  re-sealed secrets, when step 7 says so) before expecting them in the cluster.

.PARAMETER NoPrompt
  Never stop to ask you to push re-sealed secrets (for unattended runs).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\up.ps1
#>
param([switch]$NoPrompt)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "lib\common.ps1")
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$ArgoCdChartVersion = "10.10.2"     # Helm chart argo/argo-cd
$HostNames = "forge.localhost", "forge-dev.localhost", "grafana.localhost", "argocd.localhost"

# --------------------------------------------------------------------------- 1
Step "1/9 Checking prerequisites"
foreach ($tool in "docker", "k3d", "kubectl", "helm", "git", "nvidia-smi") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool is not installed or not on PATH" }
}
if (-not (Quiet "docker info")) { throw "Docker Desktop is not running. Start it and try again." }
if (-not (Quiet "nvidia-smi -L")) { throw "nvidia-smi cannot see a GPU - check the NVIDIA driver" }
foreach ($f in "cluster\Dockerfile", "cluster\k3d-config.yaml", "cluster\device-plugin.yaml",
               "gitops\bootstrap\argocd-values.yaml", "gitops\bootstrap\root-app.yaml", "gitops\apps") {
    if (-not (Test-Path (Join-Path $root $f))) { throw "missing $f - run this from a complete Forge checkout" }
}

$unresolved = $HostNames | Where-Object {
    try { [Net.Dns]::GetHostAddresses($_) | Out-Null; $false } catch { $true }
}
if ($unresolved) {
    Info "Windows does not resolve $($unresolved -join ', '); adding them to the hosts file (approve the admin prompt)"
    $addHosts = Join-Path $PSScriptRoot "add-hosts.ps1"
    try {
        if (Test-IsAdmin) { & $addHosts }
        else {
            Start-Process powershell -Verb RunAs -Wait -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$addHosts`""
        }
    } catch {
        Warn "hosts file not updated ($($_.Exception.Message)). Browsers still work; for the SDK, curl and VS Code run scripts\add-hosts.ps1 as Administrator."
    }
}

# --------------------------------------------------------------------------- 2
Step "2/9 Building the GPU node image $NodeImage (cached after the first run)"
Run "docker build -t $NodeImage cluster" "node image build failed"

# --------------------------------------------------------------------------- 3
Step "3/9 Creating the k3d cluster '$ClusterName'"
if (Quiet "k3d cluster get $ClusterName") {
    Info "cluster exists - starting it (no-op if it is running)"
    Run "k3d cluster start $ClusterName" "could not start the existing cluster"
} else {
    Run "k3d cluster create --config cluster\k3d-config.yaml" "cluster creation failed"
}
Run "kubectl config use-context $KubeContext" "kubeconfig context $KubeContext missing"
Wait-Until "the Kubernetes API" -TimeoutSec 180 { Quiet "kubectl --context $KubeContext get --raw=/readyz" } | Out-Null

# --------------------------------------------------------------------------- 4
Step "4/9 Node DNS fix and NVIDIA device plugin"
# The node image already runs this fix at every boot; copying the repo's current
# version in and running it once covers nodes created from an older image.
$dnsFix = "/bin/k3d-entrypoint-zz-dns-fallback.sh"
Run "docker cp cluster\k3d-entrypoint-zz-dns-fallback.sh ${ServerNode}:$dnsFix" "copying the DNS fix into $ServerNode failed"
Run "docker exec $ServerNode sh -c `"chmod 0755 $dnsFix && $dnsFix`"" "running the DNS fix in $ServerNode failed"
# Until CI has published sha-tagged images to GHCR, the charts use the local
# forge-*:dev images. A k3d node has its own image store, so copy them in.
$localImages = "forge-gateway:dev", "forge-web:dev", "forge-gpu-exporter:dev" | Where-Object { Quiet "docker image inspect $_" }
if ($localImages) {
    Info "importing local images into the cluster: $($localImages -join ', ')"
    Run "k3d image import -c $ClusterName $($localImages -join ' ')" "k3d image import failed"
}
Run "kubectl --context $KubeContext apply -f cluster\device-plugin.yaml" "applying the device plugin failed"
Run "kubectl --context $KubeContext -n kube-system rollout status ds/nvidia-device-plugin --timeout=300s" `
    "device plugin did not start (kubectl -n kube-system logs ds/nvidia-device-plugin)"

# --------------------------------------------------------------------------- 5
Step "5/9 Waiting until the node advertises nvidia.com/gpu"
$gpus = Wait-Until "nvidia.com/gpu on $ServerNode" -TimeoutSec 120 -IntervalSec 3 {
    Capture "kubectl --context $KubeContext get node $ServerNode -o jsonpath={.status.allocatable.nvidia\.com/gpu}"
}
Info "node has nvidia.com/gpu = $gpus"

# --------------------------------------------------------------------------- 6
Step "6/9 Installing Argo CD (chart $ArgoCdChartVersion) and the root app"
Run "helm repo add argo https://argoproj.github.io/argo-helm --force-update" "helm repo add failed"
Run ("helm upgrade --install argocd argo/argo-cd --version $ArgoCdChartVersion --kube-context $KubeContext " +
     "-n argocd --create-namespace -f gitops\bootstrap\argocd-values.yaml --wait --timeout 10m") "Argo CD install failed"
Run "kubectl --context $KubeContext apply -f gitops\bootstrap\root-app.yaml" "applying the root app failed"

# --------------------------------------------------------------------------- 7
Step "7/9 Waiting for the sealed-secrets controller, then sealing secrets if needed"
Wait-Until "the sealed-secrets controller (Argo CD deploys it first)" -TimeoutSec 900 {
    $deploys = Get-KubeJson "get deployment -A -l app.kubernetes.io/name=sealed-secrets"
    $deploys -and ($deploys.items | Where-Object { $_.status.readyReplicas -ge 1 })
} | Out-Null
& (Join-Path $PSScriptRoot "seal-secrets.ps1") -IfNeeded

$changed = Capture "git status --porcelain -- gitops/envs"
if ($changed) {
    Warn "the sealed secrets in gitops/envs changed. Argo CD reads them from GitHub, so push them:"
    Info "git add gitops/envs; git commit -m `"chore: re-seal secrets`"; git push"
    if (-not $NoPrompt -and [Environment]::UserInteractive) {
        Read-Host "    Press Enter once they are pushed" | Out-Null
    }
}
# Ask Argo CD to re-read Git now instead of at its next poll.
Quiet "kubectl --context $KubeContext -n argocd annotate applications.argoproj.io --all argocd.argoproj.io/refresh=hard --overwrite" | Out-Null

# --------------------------------------------------------------------------- 8
Step "8/9 Waiting for every Argo CD application to be Synced and Healthy (first run pulls many images)"
$appFiles = @(Join-Path $root "gitops\bootstrap\root-app.yaml") +
            @(Get-ChildItem (Join-Path $root "gitops\apps") -Filter *.yaml | ForEach-Object { $_.FullName })
$appNames = Get-ArgoAppNames $appFiles
Info "applications: $($appNames -join ', ')"
try {
    Wait-ArgoAppsHealthy $appNames -TimeoutSec 1800
} catch {
    if ($changed) { Warn "if forge-dev/forge-prod are Degraded, the re-sealed secrets are probably not pushed yet" }
    throw
}

# --------------------------------------------------------------------------- 9
Step "9/9 Forge is up"
Write-Host ""
Write-Host "  Web console (prod)  http://forge.localhost:8082"
Write-Host "  Web console (dev)   http://forge-dev.localhost:8082"
Write-Host "  OpenAI API          http://forge.localhost:8082/v1   (create a key on the 'API keys' page)"
Write-Host "  Grafana             http://grafana.localhost:8082"
Write-Host "  Argo CD             http://argocd.localhost:8082     (user: admin)"
Write-Host ""
Write-Host "  Admin password (web console): secrets\prod-admin-password.txt (prod), secrets\dev-admin-password.txt (dev)"
Write-Host "  Argo CD password: kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}'  (base64)"
Write-Host "  The GPU backend sleeps when idle; the first request wakes it (up to a few minutes on first model pull)."
