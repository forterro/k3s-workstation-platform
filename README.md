# k3s Workstation Platform

Reproducible, GitOps-managed base Kubernetes platform for an AI-oriented workstation running on
WSL2 and k3s. This repository provides the imperative seed that bootstraps the local cluster and
hands off to ArgoCD, which then reconciles the rest of the platform from git.

This repository is the generic base platform layer.

## Model in one paragraph

A minimal imperative seed (this project's Python generator) installs the strict minimum required for
ArgoCD to run: the CNI (Cilium), cert-manager, and ArgoCD itself. Everything else is declared as
per-brick umbrella charts and reconciled by ArgoCD via an app-of-apps. Updates are detected by
Renovate, flow through gitflow, and ship as semantic-version releases that you pin.

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
3. Installs and starts k3s with Cilium-ready flags (sudo, systemd service).
4. Applies the seed: Cilium, cert-manager, and ArgoCD, including the root app-of-apps.

ArgoCD then tracks the git repository set in `rootApp` (`bootstrap/helm/argo-cd/values.yaml`) and
reconciles the child Applications under `apps/`.

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
bootstrap/helm/              # seed umbrella charts (Cilium, cert-manager, argo-cd)
umbrella-charts/             # GitOps-managed, per-brick umbrella charts
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
