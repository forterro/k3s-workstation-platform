# k3s Workstation Platform

Reproducible, GitOps-managed base Kubernetes platform for an AI-oriented workstation running on
WSL2 and k3s. This repository provides the imperative seed that bootstraps the local cluster and
hands off to ArgoCD, which then reconciles the rest of the platform from git.

This repository is the generic base platform layer.

## Model in one paragraph

A minimal imperative seed (this project's Python generator) installs the strict minimum required for
ArgoCD to run: cert-manager and ArgoCD itself (k3s provides the flannel CNI). Everything else is
declared as per-brick umbrella charts and reconciled by ArgoCD via an app-of-apps. Updates are
detected by Renovate, flow through gitflow, and ship as semantic-version releases that you pin.

## Scope

The workstation is provisioned from the clone of this repository, on the machine it runs on.

## Requirements

- `uv` (Python project and dependency manager)
- A Linux host with systemd active (WSL2 with systemd enabled is the design target)
- `sudo` access: k3s and the CLI tools are installed system-wide

The bootstrap installs any missing or outdated CLI tools itself, pinned in `tool-versions.yaml`
(kubectl, helm, sops, age, step), and installs k3s if absent. Only `uv` must be present beforehand.

## Quickstart

On a fresh Ubuntu, install the prerequisites and clone the repository:

```bash
sudo apt-get update && sudo apt-get install -y git curl
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
git clone https://github.com/forterro/k3s-workstation-platform.git
cd k3s-workstation-platform
```

Then create the environment and run the bootstrap:

```bash
uv sync                                                 # create the locked environment
uv run k3s-workstation-bootstrap preflight              # report host and tooling status
uv run k3s-workstation-bootstrap bootstrap --dry-run    # preview without changing anything
uv run k3s-workstation-bootstrap bootstrap              # deploy
```

## What `bootstrap` does

`bootstrap` mutates the machine it runs on, so run it on the target workstation.

1. Installs any missing or outdated CLI tools into `/usr/local/bin` (sudo).
2. Generates a local age key for SOPS if none exists.
3. Installs and starts k3s (flannel CNI, Traefik disabled) (sudo, systemd service).
4. Applies the seed: cert-manager, the local CA material (generated on first run) and its ACME
   ClusterIssuer, then ArgoCD and the root app-of-apps.

ArgoCD then tracks the git repository set in `rootApp` (`bootstrap/helm/root-app/values.yaml`) and
reconciles the child Applications under `apps/`.

## Verify

The k3s kubeconfig is written to `/etc/rancher/k3s/k3s.yaml`:

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

kubectl get nodes            # the node should be Ready
kubectl get pods -A          # cert-manager and ArgoCD pods Running
kubectl -n argocd get application root
```

Access the ArgoCD UI:

```bash
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
kubectl -n argocd port-forward svc/argocd-server 8080:443
# then open https://localhost:8080 (user: admin)
```

## Certificates and local access

The platform runs an internal ACME certificate authority (step-ca) and issues TLS certificates
through cert-manager. The CA material is generated locally, kept under
`~/.k3s-workstation-platform/ca`, and never committed to git. The bootstrap generates it on first
run (if absent) and applies the CA ConfigMaps, Secrets and the ACME ClusterIssuer to the cluster;
the step-ca workload consumes them. To rotate the CA:

```bash
FORCE=1 make generate-ca
```

Traefik serves ingress over a LoadBalancer address (k3s servicelb assigns the node IP). Services are
exposed under the `workstation.internal` domain, which CoreDNS resolves to Traefik in-cluster (this
covers the ACME http-01 challenge). Because every service is reached through the single Traefik
address and differentiated by the HTTP host, one wildcard record covers all current and future
services.

To reach services from the Windows host, resolve `*.workstation.internal` to Traefik and trust the
CA root. This is a Windows-side, one-time setup; nothing runs in the cluster for it.

1. Enable WSL2 mirrored networking so Traefik is reachable from Windows on `localhost`. In
   `C:\Users\<you>\.wslconfig`:

   ```ini
   [wsl2]
   networkingMode=mirrored
   ```

   Then `wsl --shutdown` and reopen the distribution.

2. Install [Acrylic DNS Proxy](https://mayakron.altervista.org/support/acrylic/Home.htm) and add a
   wildcard entry to its `AcrylicHosts.txt` (it supports wildcards; the Windows hosts file does
   not):

   ```text
   127.0.0.1 *.workstation.internal
   ```

   Restart the Acrylic service, then set the network adapter's DNS server to `127.0.0.1`. Every
   `*.workstation.internal` name now resolves to Traefik with no per-service change.

3. Trust the CA root so certificates validate (PowerShell as administrator). Copy the root from the
   distribution, for example `\\wsl$\<distro>\home\<you>\.k3s-workstation-platform\ca\root_ca.crt`:

   ```powershell
   Import-Certificate -FilePath root_ca.crt -CertStoreLocation Cert:\LocalMachine\Root
   ```

Open `https://headlamp.workstation.internal` from Windows once the Headlamp certificate is issued.

## Try it in a disposable environment

The bootstrap is destructive to the host. To test it safely on WSL2, import the latest Ubuntu LTS
as a throwaway, named distribution and discard it afterwards:

```powershell
# import a base rootfs (download the latest Ubuntu LTS WSL rootfs, or `wsl --export` an existing distro)
wsl.exe --install Ubuntu-26.04
wsl -d Ubuntu-26.04
# enable systemd, then restart the distro:
#   printf '[boot]\nsystemd=true\n' | sudo tee /etc/wsl.conf
#   wsl --shutdown
# run the quickstart inside, then discard everything:
wsl --unregister Ubuntu-26.04
```

On a non-WSL Ubuntu host, Canonical Multipass gives a throwaway VM (`multipass launch`,
`multipass delete --purge`).

## Reset

To completely remove k3s and its cluster from a distribution (without discarding the distribution),
for example to re-run the bootstrap from a clean state:

```bash
uv run k3s-workstation-bootstrap reset
```

## Repository layout

```text
pyproject.toml  uv.lock  Makefile
tool-versions.yaml           # pinned CLI tool versions
src/workstation_bootstrap/   # imperative seed generator
bootstrap/helm/              # seed umbrella charts (cert-manager, argo-cd)
umbrella-charts/             # GitOps-managed, per-brick umbrella charts
secrets/                     # SOPS-encrypted secrets rendered by the KSOPS plugin
scripts/                     # operational scripts (CA generation)
apps/                        # child ArgoCD Applications reconciled by the root app-of-apps
config/                      # optional local value overrides
```

## Development

```bash
make sync      # uv sync (runtime + dev)
make lint      # ruff check
make format    # ruff format
make test      # pytest
```

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Open items

- CI workflows (gitflow, quality gates, changelog gate) are added in a dedicated increment.
