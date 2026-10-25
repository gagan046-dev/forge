{{/*
Shared template snippets for forge-inference. Object names are FIXED (ollama, redis,
gpu-exporter) instead of "<release>-<name>", because the gateways in forge-dev and
forge-prod address them by DNS name (e.g. http://ollama.forge-inference:11434).
Install exactly one release, in namespace forge-inference.
*/}}

{{- define "fi.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Selector labels: small and never changing (selectors are immutable). */}}
{{- define "fi.selectorLabels" -}}
app.kubernetes.io/name: forge-inference
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "fi.labels" -}}
{{ include "fi.selectorLabels" . }}
app.kubernetes.io/part-of: forge
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ include "fi.chart" .root }}
{{- end -}}

{{/* "registry/repository:tag" from an image block; registry "" = Docker Hub / local. */}}
{{- define "fi.image" -}}
{{- $tag := required (printf "image %s needs an explicit tag" .repository) .tag | toString -}}
{{- if .registry -}}
{{- printf "%s/%s:%s" .registry .repository $tag -}}
{{- else -}}
{{- printf "%s:%s" .repository $tag -}}
{{- end -}}
{{- end -}}

{{/* Pod securityContext for Pod Security "restricted". */}}
{{- define "fi.podSecurity" -}}
runAsNonRoot: true
runAsUser: {{ .uid }}
runAsGroup: {{ .gid | default .uid }}
{{- if .fsGroup }}
fsGroup: {{ .fsGroup }}
fsGroupChangePolicy: OnRootMismatch
{{- end }}
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/* Container securityContext for Pod Security "restricted". */}}
{{- define "fi.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/* The configured Ollama model tags, space separated (input for pull-models.sh). */}}
{{- define "fi.modelNames" -}}
{{- $names := list -}}
{{- range .Values.models -}}
{{- $names = append $names (required "every entry in models needs a name" .name) -}}
{{- end -}}
{{- join " " $names -}}
{{- end -}}

{{/*
Hash of the models list. Part of the Ollama pod template (restart on change) and of
the pull Job's name (a new Job per models list).
*/}}
{{- define "fi.modelsChecksum" -}}
{{- toJson .Values.models | sha256sum -}}
{{- end -}}

{{/* Env shared by everything that runs an Ollama binary. */}}
{{- define "fi.ollamaBaseEnv" -}}
- {name: HOME, value: /data}                      # Ollama keeps its key pair in $HOME/.ollama
- {name: OLLAMA_MODELS, value: /data/models}
{{- end -}}

{{/* NetworkPolicy peer: the gateway pods of the env namespaces. */}}
{{- define "fi.gatewayPeer" -}}
- namespaceSelector:
    matchExpressions:
      - {key: kubernetes.io/metadata.name, operator: In, values: {{ toJson .Values.networkPolicies.gatewayNamespaces }}}
  podSelector:
    matchLabels:
      {{- toYaml .Values.networkPolicies.gatewayPodLabels | nindent 6 }}
{{- end -}}
