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

{{/* Image reference; call with (dict "ctx" $ "key" "backend" "image" .Values.image.backend).
`global.azure.images.<key>` wins when set: the Azure Marketplace bundle lists
every image there and rewrites it to Microsoft's registry. Otherwise the
image's own `registry` (in-cluster PostgreSQL / Valkey), else `image.registry`;
the tag defaults to the chart's appVersion. */}}
{{- define "ap.image" -}}
{{- $azure := dig "azure" "images" .key dict (.ctx.Values.global | default dict) -}}
{{- if $azure.image -}}
{{- $ref := trimPrefix "/" (printf "%s/%s" ($azure.registry | default "") $azure.image) -}}
{{- if $azure.digest -}}
{{- printf "%s@%s" $ref $azure.digest -}}
{{- else -}}
{{- printf "%s:%s" $ref (toString (required (printf "global.azure.images.%s needs a tag or digest" .key) $azure.tag)) -}}
{{- end -}}
{{- else -}}
{{- $registry := .image.registry | default .ctx.Values.image.registry -}}
{{- $tag := default .ctx.Chart.AppVersion .image.tag -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry .image.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" .image.repository $tag -}}
{{- end -}}
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

{{/* Ingress hosts: `ingress.host` alone when set (one scalar, for settings
that cannot address list entries), else `ingress.hosts`. A JSON list. */}}
{{- define "ap.ingressHosts" -}}
{{- if .Values.ingress.host -}}
{{- list (dict "host" .Values.ingress.host) | toJson -}}
{{- else -}}
{{- .Values.ingress.hosts | default list | toJson -}}
{{- end -}}
{{- end -}}

{{/* The public URL: `publicUrl`, else the first ingress host (https when TLS
is set, by `ingress.tls` or cert-manager's `ingress.clusterIssuer`). */}}
{{- define "ap.publicUrl" -}}
{{- $hosts := include "ap.ingressHosts" . | fromJsonArray -}}
{{- if .Values.publicUrl -}}
{{- .Values.publicUrl | trimSuffix "/" -}}
{{- else if and .Values.ingress.enabled $hosts -}}
{{- $tls := or .Values.ingress.tls .Values.ingress.clusterIssuer -}}
{{- printf "%s://%s" (ternary "https" "http" (not (empty $tls))) (first $hosts).host -}}
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

{{/* "true" when the in-cluster service is on, else empty. Compares the
value as a string: Azure cluster extension settings may arrive as the
strings "true" / "false", and the string "false" is truthy in templates. */}}
{{- define "ap.postgresqlOn" -}}
{{- if eq (toString .Values.postgresql.enabled) "true" -}}true{{- end -}}
{{- end -}}

{{- define "ap.valkeyOn" -}}
{{- if eq (toString .Values.valkey.enabled) "true" -}}true{{- end -}}
{{- end -}}

{{- define "ap.postgresqlName" -}}
{{- printf "%s-postgresql" (include "ap.fullname" .) -}}
{{- end -}}

{{- define "ap.valkeyName" -}}
{{- printf "%s-valkey" (include "ap.fullname" .) -}}
{{- end -}}

{{/* A generated secret: `values.<field>` when given, else the live Secret's
`password` key (so upgrades keep it), else a new random one. Remembered in
the values map so every template in one render sees the same value. `helm
template` cannot read the live Secret: set the value there. Call with
(dict "ctx" $ "values" .Values.postgresql "field" "password" "secret" "<name>" "length" 32). */}}
{{- define "ap.keptSecret" -}}
{{- $memo := printf "_%s" .field -}}
{{- if not (hasKey .values $memo) -}}
{{- $live := lookup "v1" "Secret" .ctx.Release.Namespace .secret | default dict -}}
{{- $kept := dig "data" "password" "" $live | b64dec -}}
{{- $_ := set .values $memo (get .values .field | default $kept | default (randAlphaNum (int .length))) -}}
{{- end -}}
{{- get .values $memo -}}
{{- end -}}

{{- define "ap.postgresqlPassword" -}}
{{- include "ap.keptSecret" (dict "ctx" . "values" .Values.postgresql "field" "password" "secret" (include "ap.postgresqlName" .) "length" 32) -}}
{{- end -}}

{{- define "ap.valkeyPassword" -}}
{{- include "ap.keptSecret" (dict "ctx" . "values" .Values.valkey "field" "password" "secret" (include "ap.valkeyName" .) "length" 32) -}}
{{- end -}}

{{- define "ap.secretKeySecretName" -}}
{{- printf "%s-secret-key" (include "ap.fullname" .) -}}
{{- end -}}

{{/* APP_SECRET_KEY: `secrets.secretKey`, else (with `secrets.generateSecretKey`)
one generated on the first install and kept in its own Secret. */}}
{{- define "ap.secretKey" -}}
{{- if .Values.secrets.secretKey -}}
{{- .Values.secrets.secretKey -}}
{{- else if .Values.secrets.generateSecretKey -}}
{{- include "ap.keptSecret" (dict "ctx" . "values" .Values.secrets "field" "secretKey" "secret" (include "ap.secretKeySecretName" .) "length" 64) -}}
{{- else -}}
{{- fail "secrets.secretKey is required (or set secrets.generateSecretKey or secrets.existingSecret)" -}}
{{- end -}}
{{- end -}}

{{/* libpq URL of the in-cluster PostgreSQL; the app adds `+asyncpg`. */}}
{{- define "ap.postgresqlUrl" -}}
{{- $pg := .Values.postgresql -}}
{{- printf "postgresql://%s:%s@%s:5432/%s" (urlquery $pg.username) (urlquery (include "ap.postgresqlPassword" .)) (include "ap.postgresqlName" .) $pg.database -}}
{{- end -}}

{{/* APP_DATABASE_URL and APP_REDIS_URL: the in-cluster services when on,
else the external URLs. */}}
{{- define "ap.databaseUrl" -}}
{{- if include "ap.postgresqlOn" . -}}
{{- if .Values.database.url -}}
{{- fail "set either database.url or postgresql.enabled, not both" -}}
{{- end -}}
{{- replace "postgresql://" "postgresql+asyncpg://" (include "ap.postgresqlUrl" .) -}}
{{- else -}}
{{- required "database.url is required (or set postgresql.enabled or secrets.existingSecret)" .Values.database.url -}}
{{- end -}}
{{- end -}}

{{- define "ap.redisUrl" -}}
{{- if include "ap.valkeyOn" . -}}
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
APP_SECRET_KEY: {{ include "ap.secretKey" . | quote }}
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
