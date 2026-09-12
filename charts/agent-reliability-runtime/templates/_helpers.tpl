{{- define "agent-reliability-runtime.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "agent-reliability-runtime.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "agent-reliability-runtime.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "agent-reliability-runtime.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "agent-reliability-runtime.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "agent-reliability-runtime.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agent-reliability-runtime.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "agent-reliability-runtime.commonSecretName" -}}
{{- required "existingSecret.common is required" .Values.existingSecret.common }}
{{- end }}

{{- define "agent-reliability-runtime.migrationSecretName" -}}
{{- default (include "agent-reliability-runtime.commonSecretName" .) .Values.existingSecret.migration }}
{{- end }}

{{- define "agent-reliability-runtime.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "agent-reliability-runtime.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- /*
SEC-016: deploy by digest when one is supplied. A tag is mutable, so a
compromised registry tag can be rolled out without any chart change.
*/ -}}
{{- define "agent-reliability-runtime.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end -}}
{{- end }}
