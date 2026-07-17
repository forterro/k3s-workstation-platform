#!/usr/bin/env bash
# Generate the workstation's internal CA (root + intermediate) with an ACME provisioner and emit:
#   - public material committed in clear text:
#       umbrella-charts/core-stack/step-ca/files/{root_ca.crt,intermediate_ca.crt,ca.json}
#   - private material committed SOPS-encrypted (age):
#       secrets/step-ca/{step-ca-secrets.enc.yaml,step-ca-ca-password.enc.yaml}
#
# Run once before the first bootstrap: `make generate-ca`. Re-running rotates the CA.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
files_dir="$repo_root/umbrella-charts/core-stack/step-ca/files"
secrets_dir="$repo_root/secrets/step-ca"
age_key_file="${SOPS_AGE_KEY_FILE:-$HOME/.config/sops/age/keys.txt}"

ca_name="${CA_NAME:-Workstation Root CA}"
ca_dns="${CA_DNS:-step-ca.step-ca.svc.cluster.local,ca.workstation.internal,127.0.0.1,localhost}"
ca_address="${CA_ADDRESS:-:9000}"

for tool in step sops age-keygen; do
  command -v "$tool" >/dev/null 2>&1 || { echo "error: '$tool' is required (run the bootstrap first)" >&2; exit 1; }
done
[[ -f "$age_key_file" ]] || { echo "error: age key not found at $age_key_file" >&2; exit 1; }

age_recipient="$(age-keygen -y "$age_key_file")"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
export STEPPATH="$work"

ca_password="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)"
printf '%s' "$ca_password" > "$work/ca.pass"

echo "==> Initializing CA '$ca_name'"
step ca init \
  --name "$ca_name" \
  --dns "$ca_dns" \
  --address "$ca_address" \
  --provisioner admin \
  --password-file "$work/ca.pass" \
  --provisioner-password-file "$work/ca.pass" \
  --acme \
  --deployment-type standalone >/dev/null

ca_json="$STEPPATH/config/ca.json"

echo "==> Writing public CA material"
mkdir -p "$files_dir"
cp "$STEPPATH/certs/root_ca.crt" "$files_dir/root_ca.crt"
cp "$STEPPATH/certs/intermediate_ca.crt" "$files_dir/intermediate_ca.crt"
cp "$ca_json" "$files_dir/ca.json"

echo "==> Encrypting private CA material with SOPS (age)"
mkdir -p "$secrets_dir"

keys_plain="$work/step-ca-secrets.yaml"
{
  echo "apiVersion: v1"
  echo "kind: Secret"
  echo "metadata:"
  echo "  name: step-ca-secrets"
  echo "  namespace: step-ca"
  echo "type: smallstep.com/private-keys"
  echo "stringData:"
  echo "  intermediate_ca_key: |"
  sed 's/^/    /' "$STEPPATH/secrets/intermediate_ca_key"
  echo "  root_ca_key: |"
  sed 's/^/    /' "$STEPPATH/secrets/root_ca_key"
} > "$keys_plain"

pass_plain="$work/step-ca-ca-password.yaml"
{
  echo "apiVersion: v1"
  echo "kind: Secret"
  echo "metadata:"
  echo "  name: step-ca-ca-password"
  echo "  namespace: step-ca"
  echo "type: smallstep.com/ca-password"
  echo "stringData:"
  echo "  password: \"$ca_password\""
} > "$pass_plain"

sops --encrypt --age "$age_recipient" --encrypted-regex '^(data|stringData)$' \
  "$keys_plain" > "$secrets_dir/step-ca-secrets.enc.yaml"
sops --encrypt --age "$age_recipient" --encrypted-regex '^(data|stringData)$' \
  "$pass_plain" > "$secrets_dir/step-ca-ca-password.enc.yaml"

echo "==> Done. Root CA fingerprint:"
step certificate fingerprint "$files_dir/root_ca.crt"
echo
echo "Commit the generated files, then bootstrap (or let ArgoCD reconcile)."
