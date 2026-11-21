{{/* The image: its tag the chart's appVersion unless set. */}}
{{- define "squero.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end }}

{{/* The Contract's namespaces on this cluster: one list for every grant and setting. */}}
{{- define "squero.namespaces" -}}
{{- if not .Values.namespaces -}}
{{- fail "namespaces: list the Contract's namespaces on this cluster (the RoleBindings, the Relay's and the webhook's)" -}}
{{- end -}}
{{- end }}
