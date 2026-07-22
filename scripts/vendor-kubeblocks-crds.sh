#!/usr/bin/env bash
# Vendor the KubeBlocks CRDs into the kubeblocks umbrella chart.
#
# The KubeBlocks Helm chart does NOT ship its CustomResourceDefinitions (they are
# published as a separate GitHub release asset, kubeblocks_crds.yaml). The full
# bundle exceeds Helm's 5 MiB per-file limit, so it is split into one file per
# CRD under crds/. ArgoCD applies crds/ via `helm template --include-crds` with
# ServerSideApply (the annotation size limit forbids client-side apply here).
#
# The version is read from the kubeblocks dependency pinned in Chart.yaml, so
# run this after Renovate bumps that dependency to keep the CRDs in sync.
set -euo pipefail

chart_dir="umbrella-charts/kubeblocks-system/kubeblocks"
crds_dir="${chart_dir}/crds"
chart_yaml="${chart_dir}/Chart.yaml"

# Extract the kubeblocks dependency version (the line after `- name: kubeblocks`).
version="$(awk '/- name: kubeblocks$/{f=1;next} f&&/version:/{print $2;exit}' "${chart_yaml}")"
if [[ -z "${version}" ]]; then
  echo "error: could not read kubeblocks dependency version from ${chart_yaml}" >&2
  exit 1
fi

url="https://github.com/apecloud/kubeblocks/releases/download/v${version}/kubeblocks_crds.yaml"
echo "==> Fetching KubeBlocks CRDs v${version}"

tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
curl -sSL -o "${tmp}" "${url}"

rm -rf "${crds_dir}"
mkdir -p "${crds_dir}"

# Split the multi-document bundle into one file per CRD (named by metadata.name)
# so every file stays under Helm's 5 MiB limit.
awk -v out="${crds_dir}" '
/^---[[:space:]]*$/ { if (buf!="") { fn=out"/"name".yaml"; printf "%s", buf > fn; close(fn) } buf=""; name=""; next }
{ buf=buf $0 "\n"; if (name=="" && $1=="name:") { name=$2 } }
END { if (buf!="") { fn=out"/"name".yaml"; printf "%s", buf > fn; close(fn) } }
' "${tmp}"

echo "==> Wrote $(ls "${crds_dir}"/*.yaml | wc -l | tr -d ' ') CRD files to ${crds_dir}"
