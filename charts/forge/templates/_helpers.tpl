{{/*
Shared template snippets for the forge chart.
Usage: include "forge.labels" (dict "root" . "component" "gateway")
*/}}

{{- define "forge.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Selector labels: small and never changing (selectors are immutable).
     forge-inference's NetworkPolicies match gateway pods on name=forge + component=gateway. */}}
{{- define "forge.selectorLabels" -}}
app.kubernetes.io/name: forge
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "forge.labels" -}}
{{ include "forge.selectorLabels" . }}
app.kubernetes.io/part-of: forge
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ include "forge.chart" .root }}
{{- end -}}

{{/* "registry/repository:tag" from an image block; registry "" = Docker Hub / local. */}}
{{- define "forge.image" -}}
{{- $tag := required (printf "image %s needs an explicit tag" .repository) .tag | toString -}}
{{- if .registry -}}
{{- printf "%s/%s:%s" .registry .repository $tag -}}
{{- else -}}
{{- printf "%s:%s" .repository $tag -}}
{{- end -}}
{{- end -}}

{{/* Pod securityContext for Pod Security "restricted". */}}
{{- define "forge.podSecurity" -}}
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

{{/* Container securityContext for Pod Security "restricted", read-only root. */}}
{{- define "forge.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{- define "forge.imagePullSecrets" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
FORGE_DATABASE_URL, built from the Postgres password in the Secret. Kubernetes expands
$(VAR) from env entries defined EARLIER in the same list, so the password never appears
in a manifest or ConfigMap.
*/}}
{{- define "forge.envDatabase" -}}
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef: {name: {{ .Values.secrets.name }}, key: postgres-password}
- name: FORGE_DATABASE_URL
  value: {{ printf "postgresql+asyncpg://%s:$(POSTGRES_PASSWORD)@forge-postgres.%s.svc:5432/%s" .Values.postgres.user .Release.Namespace .Values.postgres.database | quote }}
{{- end -}}

{{/* All secret-backed env vars of the gateway. */}}
{{- define "forge.envGatewaySecrets" -}}
{{ include "forge.envDatabase" . }}
- name: REDIS_PASSWORD
  valueFrom:
    secretKeyRef: {name: {{ .Values.secrets.redisName }}, key: redis-password}
- name: FORGE_REDIS_URL
  value: {{ printf "redis://:$(REDIS_PASSWORD)@%s:%d/%d" .Values.inference.redisHost (int .Values.inference.redisPort) (int .Values.inference.redisDb) | quote }}
- name: FORGE_ADMIN_PASSWORD
  valueFrom:
    secretKeyRef: {name: {{ .Values.secrets.name }}, key: admin-password}
- name: FORGE_SESSION_SECRET
  valueFrom:
    secretKeyRef: {name: {{ .Values.secrets.name }}, key: session-secret}
{{- end -}}
