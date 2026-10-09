{{/* ───────────── names & labels ───────────── */}}

{{- define "caliban.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "caliban.fullname" -}}
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

{{- define "caliban.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "caliban.selectorLabels" -}}
app.kubernetes.io/name: {{ include "caliban.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "caliban.labels" -}}
helm.sh/chart: {{ include "caliban.chart" . }}
{{ include "caliban.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: caliban
{{- end -}}

{{- define "caliban.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "caliban.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* ───────────── image ───────────── */}}

{{- define "caliban.image" -}}
{{- $registry := .Values.image.registry -}}
{{- if .Values.global.imageRegistry -}}{{- $registry = .Values.global.imageRegistry -}}{{- end -}}
{{- $repo := .Values.image.repository -}}
{{- $ref := "" -}}
{{- if .Values.image.digest -}}
{{- $ref = printf "@%s" .Values.image.digest -}}
{{- else -}}
{{- $ref = printf ":%s" (default .Chart.AppVersion .Values.image.tag | toString) -}}
{{- end -}}
{{- if $registry -}}
{{- printf "%s/%s%s" (trimSuffix "/" $registry) $repo $ref -}}
{{- else -}}
{{- printf "%s%s" $repo $ref -}}
{{- end -}}
{{- end -}}

{{/* ───────────── secrets ───────────── */}}

{{- define "caliban.authSecretName" -}}
{{- if .Values.auth.create -}}
{{- printf "%s-auth" (include "caliban.fullname" .) -}}
{{- else -}}
{{- required "auth.existingSecret is required (or set auth.create=true for dev)" .Values.auth.existingSecret -}}
{{- end -}}
{{- end -}}

{{/* ───────────── TOML rendering ─────────────
  Values under .Values.config are rendered as TOML:
    scalar / list of scalars      -> key = value
    { env = X } / { file = X }    -> inline table (secret reference)
    other map                     -> [table]
    list of maps                  -> [[array.of.tables]]
  Within a table: plain keys first, then sub-tables, then arrays of tables (TOML order rules).
  Whole floats are rendered as integers (Helm parses every YAML number as float64), so
  write explicit decimals only where they matter; serde accepts integers for f64 fields.
*/}}

{{- define "caliban.toml.isSecretRef" -}}
{{- if and (kindIs "map" .) (eq (len .) 1) (or (hasKey . "env") (hasKey . "file")) -}}true{{- end -}}
{{- end -}}

{{- define "caliban.toml.isTable" -}}
{{- if and (kindIs "map" .) (not (include "caliban.toml.isSecretRef" .)) -}}true{{- end -}}
{{- end -}}

{{- define "caliban.toml.isTableArray" -}}
{{- if and (kindIs "slice" .) (gt (len .) 0) -}}
{{- if kindIs "map" (first .) -}}true{{- end -}}
{{- end -}}
{{- end -}}

{{- define "caliban.toml.value" -}}
{{- $v := . -}}
{{- if kindIs "string" $v -}}
{{- toJson $v -}}
{{- else if kindIs "bool" $v -}}
{{- ternary "true" "false" $v -}}
{{- else if or (kindIs "float64" $v) (kindIs "float32" $v) -}}
{{- $i := int64 $v -}}
{{- if eq (float64 $i) (float64 $v) -}}{{- $i -}}{{- else -}}{{- $v -}}{{- end -}}
{{- else if or (kindIs "int" $v) (kindIs "int64" $v) (kindIs "int32" $v) (kindIs "uint64" $v) -}}
{{- $v -}}
{{- else if kindIs "slice" $v -}}
{{- print "[" -}}
{{- range $i, $e := $v -}}{{- if $i -}}{{- print ", " -}}{{- end -}}{{- include "caliban.toml.value" $e -}}{{- end -}}
{{- print "]" -}}
{{- else if kindIs "map" $v -}}
{{- print "{ " -}}
{{- range $i, $k := keys $v | sortAlpha -}}{{- if $i -}}{{- print ", " -}}{{- end -}}{{- $k -}}{{- print " = " -}}{{- include "caliban.toml.value" (index $v $k) -}}{{- end -}}
{{- print " }" -}}
{{- else if kindIs "invalid" $v -}}
{{- fail "caliban.toml: null values are not representable in TOML; remove the key instead" -}}
{{- else -}}
{{- toJson $v -}}
{{- end -}}
{{- end -}}

{{- define "caliban.toml.table" -}}
{{- $prefix := .prefix -}}
{{- $data := .data -}}
{{- range $k := keys $data | sortAlpha -}}
{{- $v := index $data $k -}}
{{- if not (or (include "caliban.toml.isTable" $v) (include "caliban.toml.isTableArray" $v)) }}
{{ $k }} = {{ include "caliban.toml.value" $v }}
{{- end -}}
{{- end -}}
{{- range $k := keys $data | sortAlpha -}}
{{- $v := index $data $k -}}
{{- if include "caliban.toml.isTable" $v -}}
{{- $path := ternary $k (printf "%s.%s" $prefix $k) (eq $prefix "") }}

[{{ $path }}]
{{- include "caliban.toml.table" (dict "prefix" $path "data" $v) -}}
{{- end -}}
{{- end -}}
{{- range $k := keys $data | sortAlpha -}}
{{- $v := index $data $k -}}
{{- if include "caliban.toml.isTableArray" $v -}}
{{- $path := ternary $k (printf "%s.%s" $prefix $k) (eq $prefix "") -}}
{{- range $item := $v }}

[[{{ $path }}]]
{{- include "caliban.toml.table" (dict "prefix" $path "data" $item) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "caliban.toml" -}}
# Rendered by the caliban Helm chart from .Values.config. Do not edit in-cluster.
{{- include "caliban.toml.table" (dict "prefix" "" "data" .Values.config) }}
{{ end -}}

{{/* ───────────── shared pod / deployment ─────────────
  Args (dict):
    root       top-level context
    component  router | control-plane | standalone
    values     the component's values block
*/}}

{{- define "caliban.componentLabels" -}}
{{ include "caliban.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "caliban.deployment" -}}
{{- $root := .root -}}
{{- $c := .component -}}
{{- $cv := .values -}}
{{- $ports := list -}}
{{- if or (eq $c "router") (eq $c "standalone") -}}{{- $ports = append $ports (dict "name" "http" "port" 8080) -}}{{- end -}}
{{- if or (eq $c "control-plane") (eq $c "standalone") -}}{{- $ports = append $ports (dict "name" "admin" "port" 8081) -}}{{- end -}}
{{- $live := dict "path" "/healthz" "port" "http" -}}
{{- $ready := dict "path" "/healthz" "port" "http" -}}
{{- if eq $c "control-plane" -}}
{{- $live = dict "path" "/api/v1/health" "port" "admin" -}}
{{- $ready = $live -}}
{{- else if eq $c "standalone" -}}
{{- $ready = dict "path" "/api/v1/health" "port" "admin" -}}
{{- end -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "caliban.fullname" $root }}-{{ $c }}
  labels:
    {{- include "caliban.labels" $root | nindent 4 }}
    app.kubernetes.io/component: {{ $c }}
spec:
  {{- $hpa := and (eq $c "router") (dig "autoscaling" "enabled" false $cv) }}
  {{- if not $hpa }}
  replicas: {{ $cv.replicas }}
  {{- end }}
  revisionHistoryLimit: 5
  selector:
    matchLabels:
      {{- include "caliban.componentLabels" (dict "root" $root "component" $c) | nindent 6 }}
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 1
  template:
    metadata:
      labels:
        {{- include "caliban.labels" $root | nindent 8 }}
        app.kubernetes.io/component: {{ $c }}
        {{- with $cv.podLabels }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      annotations:
        checksum/config: {{ include "caliban.toml" $root | sha256sum }}
        {{- with $cv.podAnnotations }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
    spec:
      serviceAccountName: {{ include "caliban.serviceAccountName" $root }}
      automountServiceAccountToken: {{ $root.Values.serviceAccount.automountServiceAccountToken }}
      enableServiceLinks: false
      terminationGracePeriodSeconds: {{ $cv.terminationGracePeriodSeconds | default 30 }}
      {{- with $root.Values.global.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      securityContext:
        {{- toYaml $root.Values.podSecurityContext | nindent 8 }}
      containers:
        - name: caliban
          image: {{ include "caliban.image" $root | quote }}
          imagePullPolicy: {{ $root.Values.image.pullPolicy }}
          args:
            {{- toYaml $cv.args | nindent 12 }}
          ports:
            {{- range $ports }}
            - name: {{ .name }}
              containerPort: {{ .port }}
              protocol: TCP
            {{- end }}
          env:
            - name: CALIBAN_CONFIG
              value: /etc/caliban/caliban.toml
            - name: CALIBAN_WEB_DIR
              value: /usr/share/caliban/web
            - name: CALIBAN_LOG
              value: {{ $root.Values.log | quote }}
            # TODO(core): the router does not need the admin token once config loading
            # resolves secret refs lazily per component; drop it from router pods then.
            - name: CALIBAN_ADMIN_TOKEN
              valueFrom:
                secretKeyRef:
                  name: {{ include "caliban.authSecretName" $root }}
                  key: {{ $root.Values.auth.adminTokenKey }}
            - name: CALIBAN_KEK
              valueFrom:
                secretKeyRef:
                  name: {{ include "caliban.authSecretName" $root }}
                  key: {{ $root.Values.auth.kekKey }}
            - name: CALIBAN_DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: {{ required "database.existingSecret is required" $root.Values.database.existingSecret }}
                  key: {{ $root.Values.database.urlKey }}
            - name: CALIBAN_QDRANT_URL
              value: {{ $root.Values.qdrant.url | quote }}
            {{- if $root.Values.valkey.existingSecret }}
            - name: CALIBAN_VALKEY_URL
              valueFrom:
                secretKeyRef:
                  name: {{ $root.Values.valkey.existingSecret }}
                  key: {{ $root.Values.valkey.urlKey }}
            {{- else }}
            - name: CALIBAN_VALKEY_URL
              value: {{ $root.Values.valkey.url | quote }}
            {{- end }}
            {{- with $root.Values.otel.endpoint }}
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: {{ . | quote }}
            {{- end }}
            {{- with $root.Values.extraEnv }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
          {{- if or $root.Values.providerKeys.existingSecret $root.Values.extraEnvFrom }}
          envFrom:
            {{- with $root.Values.providerKeys.existingSecret }}
            - secretRef:
                name: {{ . }}
            {{- end }}
            {{- with $root.Values.extraEnvFrom }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
          {{- end }}
          startupProbe:
            httpGet: { path: {{ $live.path }}, port: {{ $live.port }} }
            {{- toYaml $root.Values.probes.startup | nindent 12 }}
          livenessProbe:
            httpGet: { path: {{ $live.path }}, port: {{ $live.port }} }
            {{- toYaml $root.Values.probes.liveness | nindent 12 }}
          readinessProbe:
            httpGet: { path: {{ $ready.path }}, port: {{ $ready.port }} }
            {{- toYaml $root.Values.probes.readiness | nindent 12 }}
          resources:
            {{- toYaml $cv.resources | nindent 12 }}
          securityContext:
            {{- toYaml $root.Values.containerSecurityContext | nindent 12 }}
          volumeMounts:
            - name: config
              mountPath: /etc/caliban
              readOnly: true
            - name: tmp
              mountPath: /tmp
            {{- with $root.Values.extraVolumeMounts }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
      volumes:
        - name: config
          configMap:
            name: {{ include "caliban.fullname" $root }}-config
            items:
              - key: caliban.toml
                path: caliban.toml
        - name: tmp
          emptyDir:
            sizeLimit: 64Mi
        {{- with $root.Values.extraVolumes }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      {{- with $cv.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $cv.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $cv.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $cv.topologySpreadConstraints }}
      topologySpreadConstraints:
        {{- toYaml . | nindent 8 }}
      {{- end }}
{{- end -}}
