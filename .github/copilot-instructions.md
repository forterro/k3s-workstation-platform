# k3s Workstation Platform — working state and continuation notes

Context for continuing work across machines. This repository bootstraps a WSL2 + k3s workstation
imperatively (a Python seed) and then hands off to ArgoCD (app-of-apps) for GitOps reconciliation.

## Architecture (as built)

- Substrate: WSL2 (Ubuntu latest LTS, systemd) + k3s (flannel CNI, Traefik disabled and servicelb
  disabled at k3s level; MetalLB owns LoadBalancer services).
- Seed generator: `src/workstation_bootstrap/` (uv project, console script `k3s-workstation-bootstrap`).
- Single source of truth: every workload chart lives under `umbrella-charts/` (grouped `core-stack/`,
  `kube-mgmt/`). The seed installs the minimal subset it needs from there directly (same chart path
  ArgoCD later adopts); `bootstrap/helm/` holds only `root-app` (the app-of-apps entrypoint glue).
- Seed phases (ordered), in `phases.py` `PHASE_PLAN`:
  1. `k3s` — install/start k3s (`--disable=traefik --disable=servicelb`).
  2. `cert-manager` — install `umbrella-charts/core-stack/cert-manager`.
  3. `step-ca-material` — generate the local CA if absent and apply CA ConfigMaps/Secrets + the
     ACME ClusterIssuer imperatively (see "CA is local" below).
  4. `argocd` — create the `argocd/sops-age` secret, then install `umbrella-charts/kube-mgmt/argocd`
     with `--set ingressRoute.enabled=false` (Traefik IngressRoute CRD absent at seed time; ArgoCD
     re-enables the ingress once it self-manages the app). Ships the KSOPS plugin as a repo-server
     init container; generic secret infra, not used by the CA anymore.
  5. `metallb` — install `umbrella-charts/core-stack/metallb` then apply a single-address
     IPAddressPool + L2Advertisement imperatively from the configured Traefik IP.
  6. `root-app` — install `bootstrap/helm/root-app` (app-of-apps; root app recurses `apps/`).
- GitOps bricks are per-brick umbrella charts under `umbrella-charts/`, each with a child ArgoCD
  Application in `apps/`. The seed-installed charts (cert-manager, metallb, argocd) are then adopted
  and reconciled by their own `apps/` manifests, so nothing stays orphaned outside GitOps. Umbrella
  dependencies are vendored (Chart.lock + charts/*.tgz committed); regenerate with `make deps`
  (needed on Renovate bumps).

## Bricks currently in git

- `umbrella-charts/core-stack/cert-manager` — cert-manager + CRDs. Seed-installed (phase 2) and then
  reconciled by `apps/cert-manager.yaml` (sync-wave -1, CreateNamespace).
- `umbrella-charts/core-stack/metallb` — MetalLB (L2). Seed-installed (phase 5); the single-address
  IPAddressPool + L2Advertisement stay imperative (machine-specific IP, IgnoreExtraneous). The
  workload is reconciled by `apps/metallb.yaml` (sync-wave -1, CreateNamespace).
- `umbrella-charts/kube-mgmt/argocd` — the ArgoCD workload (argo-cd subchart, server.insecure) plus a
  Traefik IngressRoute + step-ca Certificate on `argocd.workstation.internal`. Seed-installed with
  the ingress disabled; `apps/argocd.yaml` makes ArgoCD self-manage and enable the ingress.
- `umbrella-charts/core-stack/traefik` — Traefik, `service.type=LoadBalancer` annotated
  `metallb.io/address-pool: workstation-pool` so MetalLB pins it to the fixed Traefik IP.
- `umbrella-charts/core-stack/step-ca` — step-certificates in `existingSecrets` mode (workload only;
  no CA material in git). Consumes ConfigMaps `step-ca-certs`/`step-ca-config` and Secrets
  `step-ca-secrets`/`step-ca-ca-password` created imperatively by the bootstrap.
- `umbrella-charts/core-stack/workstation-dns` — in-cluster CoreDNS rewrite (`coredns-custom`
  ConfigMap) mapping `*.workstation.internal` to the Traefik service; required for the ACME http-01
  challenge to resolve inside the cluster. No host ports.
- `umbrella-charts/kube-mgmt/headlamp` — Headlamp + a cert-manager `Certificate` and a Traefik
  `IngressRoute` (TLS) on `headlamp.workstation.internal`.
- `umbrella-charts/kubeblocks-system/kubeblocks` — the KubeBlocks database operator (apecloud) plus
  the `postgresql`, `redis`, `mongodb` and `qdrant` engine addons, wrapping the upstream `kubeblocks`
  + engine charts from https://apecloud.github.io/helm-charts (generic mechanism mirrored from the
  datacentre, stripped of its Vault/external-secrets, S3 backup repo and image pull secret specifics). Reconciled by
  `apps/kubeblocks.yaml` (ns `kb-system`, sync-wave -1, ServerSideApply + CreateNamespace + retry).
  Provides the external Postgres for stateful workloads (e.g. the LiteLLM UI). The KubeBlocks Helm
  chart does NOT ship its CRDs and the release bundle exceeds Helm's 5 MiB per-file limit, so the
  CRDs are vendored split one-file-per-CRD under the chart `crds/` dir by `make kubeblocks-crds`
  (`scripts/vendor-kubeblocks-crds.sh`, version read from the pinned `kubeblocks` dependency); refresh
  them when Renovate bumps that dependency. `make deps` vendors the operator + addon tgz.

## Fixed Traefik LoadBalancer IP (MetalLB)

- servicelb (klipper) is disabled (`k3s.py K3S_EXEC_FLAGS`) and MetalLB (L2) assigns LoadBalancer IPs.
  Changing k3s flags requires a k3s reinstall (`reset` + `bootstrap`) on an existing cluster.
- The Traefik IP is stored as `loadbalancer_ip` in `~/.k3s-workstation-platform/config.yaml`. The
  `metallb` phase calls `settings.ensure_loadbalancer_ip` which prompts for it when unset (or takes
  `bootstrap --loadbalancer-ip`), then applies a single-address IPAddressPool `workstation-pool` +
  L2Advertisement `workstation-l2` in `metallb-system` (`_metallb_pool_manifest`). The single-address
  pool plus the Traefik service annotation pins the address to Traefik.
- `k3s-workstation-bootstrap set-loadbalancer-ip <ip>` (`phases.reconfigure_loadbalancer_ip`) persists
  the new IP, re-applies the pool, and `kubectl -n traefik rollout restart deployment`.
- MetalLB is seed-installed (phase 5) from `umbrella-charts/core-stack/metallb` and reconciled by
  `apps/metallb.yaml`; the single-address pool stays imperative (machine-specific IP,
  IgnoreExtraneous). The IP must stay inside the WSL2 NAT subnet.

## CA is local — never commit it (hard requirement)

- CA material lives ONLY at `~/.k3s-workstation-platform/ca/` (root/intermediate certs+keys, ca.json,
  ca.pass). It is generated by `scripts/generate-ca.sh` (`make generate-ca`; `FORCE=1` to rotate) and
  never enters git. `.gitignore` guards `umbrella-charts/core-stack/step-ca/files/`, `**/*_ca_key`,
  `**/ca.pass`.
- The bootstrap `step-ca-material` phase generates the CA if absent, then applies the ConfigMaps,
  Secrets and the `step-ca-acme` ClusterIssuer imperatively (kubectl create --dry-run | apply), each
  annotated `argocd.argoproj.io/compare-options: IgnoreExtraneous`.
- The KSOPS plugin + `sops-age` secret + `.sops.yaml` are kept as generic secret infra for future
  GitOps secrets, but the CA does not use them.

## Status — working and validated on the cluster

- Full seed runs; step-ca pod serves HTTPS on :9000; service `step-ca` (ClusterIP 443 -> 9000).
- ClusterIssuer `step-ca-acme` Ready; Headlamp `Certificate` issued via ACME http-01 (order valid).
- The full TLS chain works end to end in-cluster.

## Gotchas already fixed (do not regress)

- step-ca boot failure "mkdir /tmp/tmp.XXX/db": `step ca init` writes absolute STEPPATH paths into
  ca.json. `_normalize_ca_json` (phases.py) rewrites root/crt/key/db to `/home/step/...` at apply
  time (idempotent, self-heals old CAs); `generate-ca.sh` also rewrites at generation.
- ArgoCD pruning the imperatively-managed CA resources: fixed with the `IgnoreExtraneous` annotation.
- step-ca app perpetually OutOfSync: the API server defaults apiVersion/kind onto StatefulSet
  `volumeClaimTemplates`; `apps/step-ca.yaml` has an `ignoreDifferences` for those fields only.
- Do NOT expose an in-cluster DNS on a LoadBalancer port 53: k3s servicelb (klipper) grabs node port
  53 and breaks the WSL distribution's DNS. A host-facing DNS LoadBalancer was added then reverted
  (commits 47073c5 / 07867c4). Host-side resolution must be done on Windows, not in the cluster.

## Host access to `*.workstation.internal`

Resolution is done on Windows only (Acrylic). The WSL distribution keeps its DEFAULT DNS config:
`systemd-resolved` enabled, `generateResolvConf` on, WSL-managed `/etc/resolv.conf`. Do NOT mask
systemd-resolved or point resolv.conf at the cluster CoreDNS: that was tried on 2026-07-20 and
reverted because it makes WSL DNS depend on the cluster being up (breaks/slows the bootstrap on a
fresh machine, since DNS is needed before CoreDNS exists) for the sole benefit of resolving the zone
from inside the distro, which nothing in the platform requires. In-cluster clients (e.g. the ACME
http-01 challenge) already resolve the zone via the `coredns-custom` rewrite regardless.

- From the Windows host (Acrylic path, validated 2026-07-20): keep default WSL2 NAT networking (NOT
  mirrored). The fixed Traefik IP (MetalLB `loadbalancer_ip`, a `172.x` NAT address; Traefik
  `EXTERNAL-IP` equals it) is reachable from Windows and answers on :443 (`curl.exe -k
  https://<traefikIP> -H "Host: headlamp.workstation.internal"` -> HTTP 200). The in-cluster CoreDNS
  answer (Traefik ClusterIP) is not routable from Windows; the Windows side resolves the name to the
  Traefik LoadBalancer IP instead.

Windows-side setup:
1. Default NAT networking (no `.wslconfig` mirrored/ignoredPorts lines). The Traefik IP is fixed via
   MetalLB (`loadbalancer_ip`), but it must stay inside the WSL2 NAT subnet; if an HNS reset moves the
   subnet, run `set-loadbalancer-ip` with an address in the new range and update the Acrylic entry.
2. Acrylic DNS: `<traefikIP> *.workstation.internal` in `AcrylicHosts.txt` (e.g. `172.17.47.200`).
3. Acrylic MUST bind the loopback specifically: `LocalIPv4BindingAddress=127.0.0.1` in
   `AcrylicConfiguration.ini`. Reason: WSL2 NAT uses the Windows ICS service (`SharedAccess`) which
   already holds `0.0.0.0:53` on IPv4. On `0.0.0.0` Acrylic only wins IPv6 UDP 53, so IPv4 loopback
   queries hit SharedAccess and get no answer ("No response from server"). Binding the specific
   `127.0.0.1` takes precedence over the ICS wildcard for loopback. Verify with
   `Get-NetUDPEndpoint -LocalPort 53` (127.0.0.1 -> AcrylicService).
4. NRPT rule: `Add-DnsClientNrptRule -Namespace ".workstation.internal" -NameServers "127.0.0.1"`.
5. Import `~/.k3s-workstation-platform/ca/root_ca.crt` into the Windows trust store.

History (2026-07-20): first attempted WSL2 mirrored networking + Acrylic, which failed on the shared
port 53 (systemd-resolved shadowing on the WSL side, ICS on the Windows side). Reverted to NAT; then
the only remaining blocker was ICS holding IPv4 `0.0.0.0:53`, fixed by binding Acrylic to
`127.0.0.1`. A CoreDNS LoadBalancer for Windows was considered and rejected: CoreDNS returns the
Traefik ClusterIP (not Windows-routable) and reaching it needs the same Traefik IP Acrylic would
return anyway, so it adds a port-53 LoadBalancer and a rewrite change for no benefit.

## Resume testing (new machine)

```bash
git clone https://github.com/forterro/k3s-workstation-platform.git
cd k3s-workstation-platform
uv sync
uv run k3s-workstation-bootstrap preflight
uv run k3s-workstation-bootstrap bootstrap        # generates the local CA and prompts for the Traefik IP on first run
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl -n argocd get applications
kubectl -n metallb-system get ipaddresspool workstation-pool
kubectl -n step-ca get pods
kubectl get clusterissuer step-ca-acme
kubectl -n headlamp get certificate
kubectl -n traefik get svc traefik   # EXTERNAL-IP equals the configured loadbalancer_ip
```

## Conventions

- All code, comments and docs in English. No em-dash characters.
- Ask before commit/push; use conventional commit messages. HTTPS git remotes.
- Validate with `make lint` (ruff) and `make test` (pytest) before committing.
