# Forge: Command Reference

Run from `C:\Users\gagan\Desktop\AIOPs\forge` in PowerShell unless noted. Context name: `k3d-forge`. Namespaces: `forge-prod`, `forge-dev`, `forge-inference`, `monitoring`, `argocd`, `keda`, `kube-system`.

Convenience for the session:

```powershell
$ctx = "k3d-forge"
$env:FORGE_API_KEY = "fk_..."          # a key created on the API keys page
$base = "http://forge.localhost:8082"
```

---

## 1. Setup and teardown

| Goal | Command |
|---|---|
| Everything from zero (idempotent) | `powershell -ExecutionPolicy Bypass -File scripts\up.ps1` |
| Same, never wait for input | `powershell -ExecutionPolicy Bypass -File scripts\up.ps1 -NoPrompt` |
| Hosts entries (Administrator PowerShell) | `powershell -ExecutionPolicy Bypass -File scripts\add-hosts.ps1` |
| Delete the cluster (keeps `secrets\`) | `powershell -ExecutionPolicy Bypass -File scripts\down.ps1` |
| Also delete the node image | `powershell -ExecutionPolicy Bypass -File scripts\down.ps1 -Purge` |
| Stop / start cluster without deleting | `k3d cluster stop forge` / `k3d cluster start forge` |
| Re-seal secrets only | `powershell -ExecutionPolicy Bypass -File scripts\seal-secrets.ps1` |

`down.ps1` deletes models, Postgres data (keys, usage, chats, indexed repos) and the sealed-secrets key. After the next `up.ps1`, push the re-sealed files:

```powershell
git add gitops/envs; git commit -m "chore: re-seal secrets"; git push
```

### Pause vs delete: read this before "closing everything"

| You want to | Do this | What survives |
|---|---|---|
| **Pause** (free RAM, keep everything) | `k3d cluster stop forge`, then quit Docker Desktop, then `wsl --shutdown` | Everything: models, Postgres data, workspaces, sealed-secrets key |
| **Resume after a pause** | Start Docker Desktop, then `k3d cluster start forge` (or `up.ps1`, which also starts an existing cluster) | |
| **Delete** (start clean) | `scripts\down.ps1` | Only `secrets\*.txt` and what is in Git. Models (~6 GB) are re-downloaded, the secrets must be re-sealed and pushed |

Free RAM after a pause, step by step (PowerShell):

```powershell
k3d cluster stop forge
Get-Process "Docker Desktop","com.docker.*" -ErrorAction SilentlyContinue | Stop-Process -Force
wsl --shutdown                      # frees the WSL2 VM memory (Docker's 16 GB)
wsl -l --running                    # "There are no running distributions" = done
```

Passwords: `secrets\prod-admin-password.txt`, `secrets\dev-admin-password.txt`. Do not paste their contents into chats or commits.

---

## 2. Cluster status

```powershell
kubectl --context $ctx get nodes
kubectl --context $ctx get node k3d-forge-server-0 -o jsonpath='{.status.allocatable.nvidia\.com/gpu}'   # expect 1
kubectl --context $ctx get pods -A
kubectl --context $ctx -n forge-inference get pods,scaledobject
kubectl --context $ctx -n forge-prod get pods,ingress
kubectl --context $ctx -n forge-dev get pods
kubectl --context $ctx get runtimeclass
kubectl --context $ctx top pods -A          # not available: metrics-server is disabled on purpose
```

---

## 3. Argo CD

UI: http://argocd.localhost:8082, user `admin`.

```powershell
# initial admin password (decode base64)
$b64 = kubectl --context $ctx -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}'
[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))

kubectl --context $ctx -n argocd get applications.argoproj.io
kubectl --context $ctx -n argocd get application forge-prod -o jsonpath='{.status.sync.status} {.status.health.status}'

# force Argo CD to re-read Git now (default poll: 60 s)
kubectl --context $ctx -n argocd annotate applications.argoproj.io --all argocd.argoproj.io/refresh=hard --overwrite

# a sync is stuck: look at the operation, then terminate it
kubectl --context $ctx -n argocd get application forge-dev -o jsonpath='{.status.operationState.message}'
kubectl --context $ctx -n argocd patch application forge-dev --type merge -p '{"operation":null}'
```

Apps: `root`, `sealed-secrets`, `keda`, `monitoring`, `forge-inference`, `forge-dev`, `forge-prod`.

---

## 4. Logs per component

```powershell
kubectl --context $ctx -n forge-prod logs deploy/forge-gateway --tail=100 -f
kubectl --context $ctx -n forge-dev  logs deploy/forge-gateway --tail=100 -f
kubectl --context $ctx -n forge-prod logs deploy/forge-web --tail=50
kubectl --context $ctx -n forge-prod logs statefulset/forge-postgres --tail=50
kubectl --context $ctx -n forge-inference logs deploy/ollama --tail=100 -f
kubectl --context $ctx -n forge-inference logs deploy/ollama -c pull-models   # init container (name: describe pod)
kubectl --context $ctx -n forge-inference get jobs                              # model pull Job is named ollama-pull-<hash>
kubectl --context $ctx -n forge-inference logs job/<that-job-name>
kubectl --context $ctx -n forge-inference logs deploy/gpu-exporter --tail=50
kubectl --context $ctx -n forge-inference logs statefulset/redis --tail=50
kubectl --context $ctx -n keda logs deploy/keda-operator --tail=100
kubectl --context $ctx -n kube-system logs ds/nvidia-device-plugin --tail=50
kubectl --context $ctx -n argocd logs deploy/argocd-application-controller --tail=100   # name may be a statefulset
```

If a name differs, list first: `kubectl --context $ctx -n <ns> get all`.

---

## 5. Using the API

Base: `http://forge.localhost:8082/v1`. Use `curl.exe` (not the PowerShell `curl` alias).

### curl

```powershell
# models
curl.exe -s $base/v1/models -H "Authorization: Bearer $env:FORGE_API_KEY"

# chat (non-streaming); -i shows x-forge-queue-ms etc.
$body = '{"model":"forge-chat","messages":[{"role":"user","content":"Say hi in 5 words"}]}'
$body | curl.exe -s -i $base/v1/chat/completions -H "Authorization: Bearer $env:FORGE_API_KEY" -H "Content-Type: application/json" --data-binary "@-"

# chat streaming (-N = no buffering)
$body = '{"model":"forge-chat","stream":true,"messages":[{"role":"user","content":"Count to 5"}]}'
$body | curl.exe -N -s $base/v1/chat/completions -H "Authorization: Bearer $env:FORGE_API_KEY" -H "Content-Type: application/json" --data-binary "@-"

# fill-in-the-middle autocomplete
$body = '{"model":"forge-autocomplete","prompt":"def add(a, b):\n    return ","suffix":"\n\nprint(add(1, 2))","max_tokens":16,"temperature":0}'
$body | curl.exe -s $base/v1/completions -H "Authorization: Bearer $env:FORGE_API_KEY" -H "Content-Type: application/json" --data-binary "@-"

# embeddings
$body = '{"model":"forge-embed","input":["hello world","goodbye"]}'
$body | curl.exe -s $base/v1/embeddings -H "Authorization: Bearer $env:FORGE_API_KEY" -H "Content-Type: application/json" --data-binary "@-"
```

### PowerShell native

```powershell
$h = @{ Authorization = "Bearer $env:FORGE_API_KEY" }
Invoke-RestMethod "$base/v1/models" -Headers $h | ConvertTo-Json -Depth 4

$payload = @{ model = "forge-chat"; messages = @(@{ role = "user"; content = "Explain a mutex in one sentence." }) } | ConvertTo-Json -Depth 5
$r = Invoke-RestMethod "$base/v1/chat/completions" -Method Post -Headers $h -ContentType "application/json" -Body $payload
$r.choices[0].message.content
$r.usage
```

### Python

```powershell
pip install -r integrations\python\requirements.txt
python integrations\python\example.py             # all demos
python integrations\python\example.py models|chat|stream|fim|embed
```

### Keys and admin (browser console)

Create, revoke and edit limits (rpm, daily tokens) on **API keys**. Usage numbers: **Usage**. GPU and queue live: **System**. The `/api/*` admin endpoints need the session cookie and `X-Forge-Csrf: 1`, so use the console, not curl.

---

## 5b. Chat attachments, Editor and Agent

All in the web console, no commands needed:

| Feature | Where | How |
|---|---|---|
| Attach files / a folder to a chat | **Chat** | **Attach files** / **Attach folder**, or drag onto the message box (24k characters max; binaries, `node_modules`, `.git`, files over 100 KB skipped) |
| Workspaces (project folders) | **Editor** (`/workspace`) | **New workspace**: empty, or paste a `https://github.com/...` URL to clone |
| Files | Editor, left panel | **+ File**, **+ Folder**, hover a row for **rename** / **×** delete; **Ctrl+S** saves |
| Agent (Cursor-style) | Editor, **Agent** tab | Describe the task, review the proposed changes (diff per file), **Apply** / **Discard**; **auto-apply** skips review |
| Single-file AI edit | Editor, **AI edit** tab | Describe a change to the open file, review the diff, **Apply**, then save |
| Git | Editor, **Git** tab | Diff, commit (author name + email), push with a fine-grained token (Contents: read and write; never stored) |
| ZIP | Editor, top bar | **Download ZIP** |

The workspace API (`/api/workspaces/...`, SPEC section 2.9) needs the admin session, like every `/api` endpoint. Check its storage:

```powershell
kubectl -n forge-prod get pvc forge-workspaces                 # Bound, 5Gi, local-path
kubectl -n forge-prod exec deploy/forge-gateway -- ls /workspaces
kubectl -n forge-prod exec deploy/forge-gateway -- du -sh /workspaces
```

---

## 6. Changing models (GitOps)

Edit `gitops\models.yaml` (alias, Ollama `name`, `role`, `contextLength` or `dimensions`), open a PR, merge. Then:

```powershell
git switch -c models/try-new-chat
# edit gitops\models.yaml
git commit -am "feat(models): switch forge-chat to ..."; git push -u origin HEAD
gh pr create --fill; gh pr merge --squash
# after merge, watch it roll out
kubectl --context $ctx -n forge-inference get jobs,pods -w
kubectl --context $ctx -n forge-inference logs job/<ollama-pull-job-name> -f
```

Rules: the autocomplete model must be a **base** (`-base`) tag; check it fits in VRAM (about 6.8 GB free). After an embedding model change, re-index all repos. If the autocomplete family changes, also edit the FIM template in `integrations\vscode\config.yaml`.

---

## 7. Promote dev to prod

```powershell
# which tag does each env run?
Get-Content charts\forge\values-dev-images.yaml  | Select-String tag
Get-Content charts\forge\values-prod-images.yaml | Select-String tag

# opens a PR copying the dev tag to values-prod-images.yaml
gh workflow run promote.yaml
gh workflow run promote.yaml -f tag=sha-1234567     # a specific tag
gh run list --workflow promote.yaml --limit 3

# review and merge the PR; Argo CD then syncs forge-prod
gh pr list
gh pr merge <number> --squash
```

Manual alternative (needed until the repo setting "Allow GitHub Actions to create and approve pull requests" is on), the way prod got `sha-fdf04f9`:

```powershell
git checkout main; git pull
$tag = (Select-String 'sha-[0-9a-f]{7}' charts\forge\values-dev-images.yaml).Matches[0].Value
git checkout -b "promote/prod-$tag"
(Get-Content charts\forge\values-prod-images.yaml) -replace 'sha-[0-9a-f]{7}', $tag | Set-Content -Encoding utf8 charts\forge\values-prod-images.yaml
git commit -am "chore(prod): promote images $tag"; git push -u origin "promote/prod-$tag"
gh pr create --base main --title "Promote $tag to prod" --body "Verified on dev."
gh pr checks --watch; gh pr merge --merge --delete-branch
# then watch prod roll out
kubectl -n argocd annotate app forge-prod argocd.argoproj.io/refresh=hard --overwrite
kubectl -n forge-prod rollout status deploy/forge-gateway; kubectl -n forge-prod rollout status deploy/forge-web
```

 CI status: `gh run list --workflow ci.yaml --limit 5`.

---

## 8. Sealing secrets

```powershell
# generate missing plain secrets in secrets\ and seal them for THIS cluster's key (never prints them)
powershell -ExecutionPolicy Bypass -File scripts\seal-secrets.ps1
# only if needed (used by up.ps1)
powershell -ExecutionPolicy Bypass -File scripts\seal-secrets.ps1 -IfNeeded

git add gitops/envs; git commit -m "chore: re-seal secrets"; git push
```

kubeseal is downloaded (pinned 0.40.0, checksum verified) to `C:\Tools\kubeseal\` when missing. Sealed files are only valid for the cluster that holds the matching controller key.

---

## 9. Load test and benchmark

```powershell
pip install -r bench\requirements.txt

# per-model tokens/s, TTFT and VRAM -> results\bench-<timestamp>.md/.json
$env:FORGE_ADMIN_PASSWORD = Get-Content secrets\prod-admin-password.txt   # optional, gives per-model VRAM
python bench\bench.py --runs 3

# mixed workload for 60 s
python bench\loadtest.py --scenario mixed --duration 60 --chat 2 --autocomplete 1 --embed 1

# priority behaviour: autocomplete p95 with and without a chat flood
python bench\loadtest.py --scenario priority --duration 40 --chat 6
```

Results are saved to `results\loadtest-*.json` unless `--no-save`.

---

## 10. GPU watching

```powershell
nvidia-smi                                  # one snapshot
nvidia-smi -l 2                             # refresh every 2 s
nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv -l 2
kubectl --context $ctx -n forge-inference exec deploy/ollama -- ollama ps    # loaded models and size
```

Grafana dashboard "Forge": http://grafana.localhost:8082. Raw exporter (port-forward):

```powershell
kubectl --context $ctx -n forge-inference port-forward svc/gpu-exporter 9400:9400
curl.exe -s http://localhost:9400/gpu
```

---

## 11. Observing scale to zero

Terminal 1 (VRAM and replicas):

```powershell
nvidia-smi --query-gpu=memory.used --format=csv -l 5
```

Terminal 2:

```powershell
kubectl --context $ctx -n forge-inference get deploy ollama -w
kubectl --context $ctx -n forge-inference get scaledobject ollama
```

Check the demand signal (port-forward the gateway first; `/internal` is not on the ingress):

```powershell
kubectl --context $ctx -n forge-prod port-forward svc/forge-gateway 8000:8000
curl.exe -s http://localhost:8000/internal/demand      # {"demand":..,"queued":..,"inflight":..,"idle_for_s":..}
```

Expected timeline after the last request: demand falls to 0 after 600 s, Ollama goes to 0 replicas about 60 s later, VRAM drops to about 0.8 GB. A new request: gateway shows `sleeping`, then `waking`, Ollama ready in about 9 s, first token about 15 s.

---

## 12. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| First request after a quiet period takes about 15 s | Ollama was scaled to zero and is reloading the models | Normal. Later requests are fast. Raise `FORGE_IDLE_SECONDS` to stay awake longer |
| `503 backend_unavailable` | Ollama did not become ready within 240 s (first model pull, crash, GPU missing) | `kubectl -n forge-inference get pods`; `describe pod`; check the pull Job and device plugin |
| `getaddrinfo ENOTFOUND forge.localhost` (Python, curl, VS Code) | Windows only resolves `*.localhost` in browsers | `scripts\add-hosts.ps1` from an Administrator PowerShell |
| `401 invalid_api_key` | Wrong, revoked or missing key | Create a new key; header must be `Authorization: Bearer fk_...` |
| `429 rate_limit_exceeded` | More requests per minute than the key's `rpm_limit` | Wait for `Retry-After`; raise the limit on the API keys page |
| `429 quota_exceeded` | Daily token quota used up | Raise the quota or wait for the next day |
| `429 queue_full` | 32 requests already waiting for the GPU | Retry after `Retry-After` (2 s); reduce concurrency |
| `504 queue_timeout` | Waited over 120 s for a GPU slot | GPU overloaded; reduce load |
| `400 context_length_exceeded` | Prompt longer than the model's context (8192 chat / 4096 autocomplete) | Shorten the prompt |
| `400 model_not_found` | Unknown alias | Use a name from `GET /v1/models` |
| `499 superseded` (autocomplete) | A newer keystroke replaced it | Expected; client should ignore |
| Autocomplete gives chat-style prose | The autocomplete model is an instruct model | Use a `-base` tag in `models.yaml` |
| Argo CD app stuck "Progressing" in forge-dev/prod; pod `CreateContainerConfigError` | A pod waits for a Secret that a later sync wave creates, or sealed secrets are for another cluster | SealedSecrets must be wave -5; terminate the stuck sync; re-run `seal-secrets.ps1` and push |
| `forge-dev`/`forge-prod` Degraded right after `up.ps1` | Re-sealed secrets not pushed, so Argo CD reads old sealed files | `git add gitops/envs; git commit; git push`, then refresh Argo apps |
| `kubeseal` unpack error mentioning a host | Git's GNU `tar` read `C:\` as a remote host | Use `C:\Windows\System32\tar.exe` (the script already does) |
| ScaledObject Degraded / `Fallback=True` | A gateway in `demandSources` is unreachable | Fix or remove that namespace in `autoscaling.demandSources` |
| Pod `ollama` Pending, `Insufficient nvidia.com/gpu` | Device plugin not running, or another pod holds the GPU | `kubectl -n kube-system get ds nvidia-device-plugin`; only one Ollama may exist (Recreate strategy) |
| `nvidia-smi` fails inside the node | WSL GPU libs not linked | Re-run `up.ps1` (step 4); see gpu-hello guide section 6 |
| Image pulls fail `lookup ...: Try again` | Docker Desktop DNS flake | `up.ps1` step 4 reinstalls the DNS fallback |
| Ask-repo stays `queued` | Another index job holds the cluster lock | Wait; lock expires if a pod died |
| Ask-repo `failed` with URL error | Host not allowed or not `https://` | Allowed hosts: github.com, gitlab.com, codeberg.org |
| Answers cite wrong files | Small embedding model limit | Ask more specifically; raise `top_k` |
| Web console shows login again | Session expired (12 h) or admin password changed | Log in again |
| Changes pushed but cluster unchanged | Argo CD reads GitHub, not disk | Push; refresh apps (section 3) |
| Cluster gone after Docker reset | Docker Desktop was reset or `down.ps1` ran | Re-run `up.ps1`, push re-sealed secrets. To only pause next time, use `k3d cluster stop forge` |
| Agent answers but no files appear | The 7B model wrote plain code instead of action blocks | Ask again ("write the files"); code blocks named by a file are picked up automatically and shown for review |
| Agent says "not a file in this project" | It tried to read a library (e.g. `sklearn.datasets`) | Harmless; it continues. Only project files can be read |
| Push fails "authentication failed" | Token missing, expired, without Contents: write, or repo does not exist | Create a fine-grained token for that repo with Contents read and write |
| Workspace clone `400 invalid_url` | Host not allowed or not `https://` | Allowed: github.com, gitlab.com, codeberg.org |
