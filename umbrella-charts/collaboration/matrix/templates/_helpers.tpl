{{- define "matrix.tuwunelConfig" -}}
[global]
server_name = {{ .Values.serverName | quote }}
database_path = "/data"
address = ["0.0.0.0"]
port = 8008
allow_federation = false
allow_registration = false
# Humans sign in through the workstation SSO only; bots are appservices (no password).
login_with_password = false
appservice_dir = "/etc/tuwunel/appservices"
{{- range $k, $v := .Values.homeserver.extraConfig }}
{{ $k }} = {{ $v | toJson }}
{{- end }}

[global.well_known]
client = {{ .Values.publicUrl | quote }}

[[global.identity_provider]]
brand = "keycloak"
name = {{ .Values.sso.name | quote }}
client_id = {{ .Values.sso.clientId | quote }}
client_secret_file = "/etc/tuwunel/oidc/client_secret"
issuer_url = {{ .Values.sso.issuerUrl | quote }}
callback_url = "{{ .Values.publicUrl }}/_matrix/client/unstable/login/sso/callback/{{ .Values.sso.clientId }}"
userid_claims = {{ .Values.sso.userIdClaims | toJson }}
# No random Matrix ID when no claim fits: the sign-in fails and the claims get fixed.
unique_id_fallbacks = false
# Our own IdP: a sign-in maps to the existing Matrix account of the same name.
trusted = true
default = true
{{- end -}}
