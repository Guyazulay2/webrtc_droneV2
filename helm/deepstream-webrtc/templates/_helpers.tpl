{{- define "app.name" -}}deepstream-webrtc{{- end }}
{{- define "app.fullname" -}}{{ include "app.name" . }}{{- end }}
{{- define "app.labels" }}
app.kubernetes.io/name: {{ include "app.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
{{- define "app.selectorLabels" }}
app: {{ include "app.name" . }}
{{- end }}
