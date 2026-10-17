{{/* Names */}}
{{- define "ap.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "ap.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Labels; call with (dict "ctx" $ "component" "backend") */}}
{{- define "ap.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ap.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "ap.labels" -}}
{{ include "ap.selectorLabels" . }}
helm.sh/chart: {{ printf "%s-%s" .ctx.Chart.Name .ctx.Chart.Version | replace "+" "_" }}
app.kubernetes.io/version: {{ .ctx.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .ctx.Release.Service }}
app.kubernetes.io/part-of: annotide
{{- end -}}

{{/* Image reference; call with (dict "ctx" $ "image" .Values.image.backend) */}}
{{- define "ap.image" -}}
{{- $registry := .ctx.Values.image.registry -}}
{{- $tag := default .ctx.Chart.AppVersion .image.tag -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry .image.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" .image.repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "ap.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "ap.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "ap.secretName" -}}
{{- default (printf "%s-env" (include "ap.fullname" .)) .Values.secrets.existingSecret -}}
{{- end -}}

{{- define "ap.backendHost" -}}
{{- printf "%s-backend" (include "ap.fullname" .) -}}
{{- end -}}

{{/* The public URL: `publicUrl`, else the first ingress host (https when TLS is set). */}}
{{- define "ap.publicUrl" -}}
{{- if .Values.publicUrl -}}
{{- .Values.publicUrl | trimSuffix "/" -}}
{{- else if and .Values.ingress.enabled .Values.ingress.hosts -}}
{{- $host := (first .Values.ingress.hosts).host -}}
{{- printf "%s://%s" (ternary "https" "http" (gt (len .Values.ingress.tls) 0)) $host -}}
{{- end -}}
{{- end -}}

{{/* Environment shared by the API, worker and migration job. */}}
{{- define "ap.envFrom" -}}
- configMapRef:
    name: {{ include "ap.fullname" . }}-config
- secretRef:
    name: {{ include "ap.secretName" . }}
{{- end -}}

{{/* Pod security context plus the image's numeric user: runAsNonRoot
cannot verify a named USER. Call with (dict "ctx" $ "uid" 1000). */}}
{{- define "ap.podSecurityContext" -}}
{{- $sc := merge (deepCopy .ctx.Values.podSecurityContext) (dict "runAsUser" .uid "runAsGroup" .uid "fsGroup" .uid) -}}
{{- toYaml $sc }}
{{- end -}}

{{- define "ap.containerSecurityContext" -}}
{{- toYaml .Values.containerSecurityContext }}
{{- end -}}

{{/* A third-party image (in-cluster PostgreSQL / Valkey); call with its values map. */}}
{{- define "ap.thirdPartyImage" -}}
{{- if .registry -}}
{{- printf "%s/%s:%s" .registry .repository .tag -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}
{{- end -}}

{{- define "ap.postgresqlName" -}}
{{- printf "%s-postgresql" (include "ap.fullname" .) -}}
{{- end -}}

{{- define "ap.valkeyName" -}}
{{- printf "%s-valkey" (include "ap.fullname" .) -}}
{{- end -}}

{{/* Password of an in-cluster service: the value given, else the live
Secret's (so upgrades keep it), else a new random one. Remembered in the
values map so every template in one render sees the same password. `helm
template` cannot read the live Secret: set the password there. Call with
(dict "ctx" $ "values" .Values.postgresql "secret" "<name>"). */}}
{{- define "ap.inClusterPassword" -}}
{{- if not (hasKey .values "_password") -}}
{{- $live := lookup "v1" "Secret" .ctx.Release.Namespace .secret | default dict -}}
{{- $kept := dig "data" "password" "" $live | b64dec -}}
{{- $_ := set .values "_password" (.values.password | default $kept | default (randAlphaNum 32)) -}}
{{- end -}}
{{- .values._password -}}
{{- end -}}

{{- define "ap.postgresqlPassword" -}}
{{- include "ap.inClusterPassword" (dict "ctx" . "values" .Values.postgresql "secret" (include "ap.postgresqlName" .)) -}}
{{- end -}}

{{- define "ap.valkeyPassword" -}}
{{- include "ap.inClusterPassword" (dict "ctx" . "values" .Values.valkey "secret" (include "ap.valkeyName" .)) -}}
{{- end -}}

{{/* libpq URL of the in-cluster PostgreSQL; the app adds `+asyncpg`. */}}
{{- define "ap.postgresqlUrl" -}}
{{- $pg := .Values.postgresql -}}
{{- printf "postgresql://%s:%s@%s:5432/%s" (urlquery $pg.username) (urlquery (include "ap.postgresqlPassword" .)) (include "ap.postgresqlName" .) $pg.database -}}
{{- end -}}

{{/* APP_DATABASE_URL and APP_REDIS_URL: the in-cluster services when on,
else the external URLs. */}}
{{- define "ap.databaseUrl" -}}
{{- if .Values.postgresql.enabled -}}
{{- if .Values.database.url -}}
{{- fail "set either database.url or postgresql.enabled, not both" -}}
{{- end -}}
{{- replace "postgresql://" "postgresql+asyncpg://" (include "ap.postgresqlUrl" .) -}}
{{- else -}}
{{- required "database.url is required (or set postgresql.enabled or secrets.existingSecret)" .Values.database.url -}}
{{- end -}}
{{- end -}}

{{- define "ap.redisUrl" -}}
{{- if .Values.valkey.enabled -}}
{{- if .Values.redis.url -}}
{{- fail "set either redis.url or valkey.enabled, not both" -}}
{{- end -}}
{{- printf "redis://:%s@%s:6379/0" (urlquery (include "ap.valkeyPassword" .)) (include "ap.valkeyName" .) -}}
{{- else -}}
{{- required "redis.url is required (or set valkey.enabled or secrets.existingSecret)" .Values.redis.url -}}
{{- end -}}
{{- end -}}

{{/* Secret keys the chart creates when no existingSecret is given. */}}
{{- define "ap.secretData" -}}
APP_DATABASE_URL: {{ include "ap.databaseUrl" . | quote }}
APP_REDIS_URL: {{ include "ap.redisUrl" . | quote }}
APP_SECRET_KEY: {{ required "secrets.secretKey is required (or set secrets.existingSecret)" .Values.secrets.secretKey | quote }}
{{- with .Values.secrets.secretKeyPrevious }}
APP_SECRET_KEY_PREVIOUS: {{ . | quote }}
{{- end }}
{{- with .Values.secrets.smtpPassword }}
APP_SMTP_PASSWORD: {{ . | quote }}
{{- end }}
{{- with .Values.secrets.oidcClientSecret }}
APP_OIDC_CLIENT_SECRET: {{ . | quote }}
{{- end }}
{{- with .Values.secrets.licenseKey }}
APP_LICENSE_KEY: {{ . | quote }}
{{- end }}
{{- range $key, $value := .Values.secrets.extra }}
{{ $key }}: {{ $value | quote }}
{{- end }}
{{- end -}}
