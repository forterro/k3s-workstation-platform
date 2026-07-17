#!/usr/bin/env bash
# Generate the workstation's internal CA (root + intermediate) with an ACME provisioner.
#
# All material stays LOCAL to the workstation and is never committed to git. It is written to
# $CA_DIR (default ~/.k3s-workstation-platform/ca) and applied to the cluster imperatively by the
# bootstrap. Re-run with FORCE=1 to rotate the CA.
set -euo pipefail

ca_dir="${CA_DIR:-$HOME/.k3s-workstation-platform/ca}"
ca_name="${CA_NAME:-Workstation Root CA}"
ca_dns="${CA_DNS:-step-ca.step-ca.svc.cluster.local,ca.workstation.internal,127.0.0.1,localhost}"
ca_address="${CA_ADDRESS:-:9000}"

command -v step >/dev/null 2>&1 || { echo "error: 'step' is required (run the bootstrap first)" >&2; exit 1; }

if [[ -f "$ca_dir/root_ca.crt" && "${FORCE:-0}" != "1" ]]; then
  echo "CA already present at $ca_dir (set FORCE=1 to rotate)"
  exit 0
fi

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

echo "==> Writing CA material to $ca_dir"
mkdir -p "$ca_dir"
chmod 700 "$ca_dir"
cp "$STEPPATH/certs/root_ca.crt" "$ca_dir/root_ca.crt"
cp "$STEPPATH/certs/intermediate_ca.crt" "$ca_dir/intermediate_ca.crt"
cp "$STEPPATH/config/ca.json" "$ca_dir/ca.json"
cp "$STEPPATH/secrets/root_ca_key" "$ca_dir/root_ca_key"
cp "$STEPPATH/secrets/intermediate_ca_key" "$ca_dir/intermediate_ca_key"
printf '%s' "$ca_password" > "$ca_dir/ca.pass"
chmod 600 "$ca_dir"/root_ca_key "$ca_dir"/intermediate_ca_key "$ca_dir"/ca.pass

echo "==> Done. Root CA fingerprint:"
step certificate fingerprint "$ca_dir/root_ca.crt"
