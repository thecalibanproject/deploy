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

{{/* ───────────── split mode: router config source ─────────────
  "snapshot" when mode=split and router.mode=snapshot (the default): routers poll signed
  snapshots from the control plane. "static": routers read the ConfigMap. "" in standalone.
*/}}

{{- define "caliban.routerMode" -}}
{{- if eq .Values.mode "split" -}}
{{- $m := .Values.router.mode | default "snapshot" -}}
{{- if not (has $m (list "snapshot" "static")) -}}
{{- fail (printf "router.mode must be 'snapshot' or 'static', got %q" $m) -}}
{{- end -}}
{{- $m -}}
{{- end -}}
{{- end -}}

{{/* Secret the control plane reads: signing key and router token. */}}
{{- define "caliban.snapshotSecretName" -}}
{{- if .Values.snapshotKeys.create -}}
{{- printf "%s-snapshot" (include "caliban.fullname" .) -}}
{{- else -}}
{{- required "snapshotKeys.existingSecret is required with router.mode=snapshot (or set router.mode=static, or snapshotKeys.create=true for dev)" .Values.snapshotKeys.existingSecret -}}
{{- end -}}
{{- end -}}

{{/* Secret the routers read: public key and router token. Defaults to the one above. */}}
{{- define "caliban.snapshotRouterSecretName" -}}
{{- if and (not .Values.snapshotKeys.create) .Values.snapshotKeys.routerExistingSecret -}}
{{- .Values.snapshotKeys.routerExistingSecret -}}
{{- else -}}
{{- include "caliban.snapshotSecretName" . -}}
{{- end -}}
{{- end -}}

{{- define "caliban.controlPlaneUrl" -}}
{{- .Values.router.snapshot.controlPlaneUrl | default (printf "http://%s-control-plane:%v" (include "caliban.fullname" .) .Values.controlPlane.service.port) -}}
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
{{- /* Split mode with router.mode=snapshot: routers take their config from the control
       plane and do not mount the ConfigMap; the control plane signs the snapshots. */ -}}
{{- $snapshot := eq (include "caliban.routerMode" $root) "snapshot" -}}
{{- $isRouter := eq $c "router" -}}
{{- $snapRouter := and $isRouter $snapshot -}}
{{- $snapCP := and (eq $c "control-plane") $snapshot -}}
{{- $useConfig := not $snapRouter -}}
{{- $snapCache := and $snapRouter $root.Values.router.snapshot.cache.enabled -}}
{{- if $snapRouter -}}
{{- $poll := int $root.Values.router.snapshot.pollIntervalSeconds -}}
{{- if lt $poll 1 -}}{{- fail "router.snapshot.pollIntervalSeconds must be >= 1" -}}{{- end -}}
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
      {{- if or $useConfig $cv.podAnnotations }}
      annotations:
        {{- if $useConfig }}
        checksum/config: {{ include "caliban.toml" $root | sha256sum }}
        {{- end }}
        {{- with $cv.podAnnotations }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
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
            {{- if $useConfig }}
            - name: CALIBAN_CONFIG
              value: /etc/caliban/caliban.toml
            {{- end }}
            - name: CALIBAN_LOG
              value: {{ $root.Values.log | quote }}
            {{- if not $isRouter }}
            # Control plane only: routers never read the admin token or the database
            # (core reads both only when it builds the control plane).
            - name: CALIBAN_WEB_DIR
              value: /usr/share/caliban/web
            - name: CALIBAN_ADMIN_TOKEN
              valueFrom:
                secretKeyRef:
                  name: {{ include "caliban.authSecretName" $root }}
                  key: {{ $root.Values.auth.adminTokenKey }}
            - name: CALIBAN_DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: {{ required "database.existingSecret is required" $root.Values.database.existingSecret }}
                  key: {{ $root.Values.database.urlKey }}
            {{- end }}
            # Every component: opens sealed BYOK credentials (they stay sealed inside
            # snapshots) and derives the per-tenant cache_salt, which must match across routers.
            - name: CALIBAN_KEK
              valueFrom:
                secretKeyRef:
                  name: {{ include "caliban.authSecretName" $root }}
                  key: {{ $root.Values.auth.kekKey }}
            {{- if $snapCP }}
            # Split mode: sign config snapshots for the routers (GET /api/v1/snapshot).
            - name: CALIBAN_SNAPSHOT_SIGNING_KEY
              valueFrom:
                secretKeyRef:
                  name: {{ include "caliban.snapshotSecretName" $root }}
                  key: {{ $root.Values.snapshotKeys.signingKeyKey }}
            - name: CALIBAN_ROUTER_TOKEN
              valueFrom:
                secretKeyRef:
                  name: {{ include "caliban.snapshotSecretName" $root }}
                  key: {{ $root.Values.snapshotKeys.routerTokenKey }}
            {{- end }}
            {{- if $snapRouter }}
            # Split mode: config comes from signed control-plane snapshots, not the ConfigMap.
            - name: CALIBAN_CONTROL_PLANE_URL
              value: {{ include "caliban.controlPlaneUrl" $root | quote }}
            - name: CALIBAN_ROUTER_TOKEN
              valueFrom:
                secretKeyRef:
                  name: {{ include "caliban.snapshotRouterSecretName" $root }}
                  key: {{ $root.Values.snapshotKeys.routerTokenKey }}
            - name: CALIBAN_SNAPSHOT_PUBLIC_KEY
              valueFrom:
                secretKeyRef:
                  name: {{ include "caliban.snapshotRouterSecretName" $root }}
                  key: {{ $root.Values.snapshotKeys.publicKeyKey }}
            - name: CALIBAN_SNAPSHOT_POLL_SECS
              value: {{ int $root.Values.router.snapshot.pollIntervalSeconds | quote }}
            {{- if $snapCache }}
            - name: CALIBAN_SNAPSHOT_CACHE
              value: /var/lib/caliban/snapshot.json
            {{- end }}
            {{- end }}
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
            {{- with $root.Values.valkey.passwordSecret }}
            - name: CALIBAN_VALKEY_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ . }}
                  key: {{ $root.Values.valkey.passwordKey }}
            {{- end }}
            {{- with $root.Values.otel.endpoint }}
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: {{ . | quote }}
            {{- end }}
            {{- with $root.Values.extraEnv }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
            {{- with $cv.extraEnv }}
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
            {{- if $useConfig }}
            - name: config
              mountPath: /etc/caliban
              readOnly: true
            {{- end }}
            - name: tmp
              mountPath: /tmp
            {{- if $snapCache }}
            - name: snapshot-cache
              mountPath: /var/lib/caliban
            {{- end }}
            {{- with $root.Values.extraVolumeMounts }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
      volumes:
        {{- if $useConfig }}
        - name: config
          configMap:
            name: {{ include "caliban.fullname" $root }}-config
            items:
              - key: caliban.toml
                path: caliban.toml
        {{- end }}
        - name: tmp
          emptyDir:
            sizeLimit: 64Mi
        {{- if $snapCache }}
        # Last good signed snapshot (written 0600, re-verified on load). An emptyDir survives
        # container restarts, so a router restarted while the control plane is down keeps
        # serving; a new pod still needs the control plane once.
        - name: snapshot-cache
          emptyDir:
            sizeLimit: {{ $root.Values.router.snapshot.cache.sizeLimit }}
        {{- end }}
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
