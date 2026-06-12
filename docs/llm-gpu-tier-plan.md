# Local-LLM GPU Tier — Implementation Plan

> Status: **planned / not implemented**. Reference design for when the hardware is
> reorganized. Created 2026-06. Engine/architecture choices validated against the
> reference implementation at <https://github.com/joryirving/home-ops>.

## Goal

Add a local-LLM serving tier to the Talos cluster (currently 4× MS-01, Intel).
Primary box: a **Framework Desktop — AMD Ryzen AI Max+ 395 "Strix Halo"**
(`gfx1151`, 128 GB unified LPDDR5x, ~256 GB/s). Optional second tier: an **NVIDIA
3090** attached to an existing MS-01 via OCuLink/Thunderbolt eGPU.

## Locked decisions

| Decision | Choice | Why |
|---|---|---|
| Inference engine | **llama.cpp server first** (vLLM later, only for real concurrency) | Reference uses llama.cpp even on NVIDIA; spec-decode + GGUF flexibility win for homelab concurrency. vLLM on `gfx1151` is nightly-ROCm-only until ROCm 8 (~mid-2026). |
| AMD GPU access | **Vulkan + `/dev/dri`, no ROCm operator** | `--device Vulkan0` sidesteps the entire ROCm-on-Talos problem. `amdgpu`/`amd-ucode` extensions provide the DRI device. |
| 3090 attach | **OCuLink/TB eGPU on an MS-01** | No separate box needed. ⚠️ OCuLink renames the host's onboard NICs (PCI topology shift) — re-pin Ethernet/Bond config. |
| Router / model registry | **LiteLLM** ConfigMap | Single source of truth for "what models exist"; failover, caching, Prometheus. |
| MCP servers | **ToolHive** (`toolhive.stacklok.dev/MCPServer` CRDs) | k8s-native, each server a reviewable CR. |
| Model storage | **`ceph-block` RWO per-model PVC** (Rook) | Persists across reschedule, no re-download. `ceph-filesystem` RWX if a shared cache is preferred. |
| Namespace | **`llm`** | Auto-discovered by Flux `cluster-apps`. |
| Secrets | External-Secrets → 1Password (`onepassword-connect`) | HF token + any cloud keys. |

## Phases

1. **Talos — Framework AMD worker.** New node block in `talos/talconfig.yaml`
   (inline `schematic`, single NIC, `userVolumes`, `nodeLabels`, `nodeTaints`,
   `kernelModules`) + OOM patch. See "talhelper node block" below.
2. **AMD device plugin.** `kubernetes/apps/kube-system/amd-device-plugin/`
   (`rocm/k8s-device-plugin`), register in the kube-system `kustomization.yaml`.
3. **`llm` namespace + first model.** `kubernetes/apps/llm/` scaffold,
   `huggingface` ExternalSecret, `llama-framework/` (app-template + llama.cpp
   `server-vulkan`, `--device Vulkan0`, privileged, `/dev/dri`, `ceph-block` PVC,
   `nodeSelector topology.kubernetes.io/gpus: amd`, toleration `llm-workload`).
4. **LiteLLM + cut over open-webui.** `kubernetes/apps/llm/litellm/` (ConfigMap
   model_list → `http://llama-framework.llm:8080/v1`, Dragonfly cache, Prometheus).
   Repoint `open-webui` from `framework.internal:11434` → `http://litellm.llm:8080/v1`.
5. **MCP via ToolHive.** Operator + per-server `MCPServer` CRs
   (`kubectl-mcp`, `github-mcp`, …); register as open-webui tool servers.
6. **NVIDIA 3090 tier (optional/later).** eGPU MS-01 schematic
   (`nvidia-open-gpu-kernel-modules-production`, `nvidia-container-toolkit-production`,
   `thunderbolt`), `nvidia-device-plugin`, `llama-nvidia/` (`server-cuda`,
   `nvidia.com/gpu: 1`). **Disturbs networking (NIC rename) — do last.**
7. **Observability & power.** Grafana llama.cpp/GPU dashboards + ServiceMonitors;
   optional KEDA scale-to-zero on the 3090 deployments.

**Critical path:** 1 → 2 → 3 → 4 gives a working in-cluster LLM. 5/6/7 are add-ons.

## talhelper Framework node block

talhelper collapses the reference's `schematic.yaml` + `skirk.yaml` + Jinja template
into one node entry. talhelper generates the Factory image from inline `schematic:`
— no manual factory URL.

```yaml
  - hostname: "framework"
    ipAddress: "10.10.40.25"
    controlPlane: false
    installDiskSelector:
      serial: "<framework-os-nvme-serial>"
    schematic:
      customization:
        extraKernelArgs:
          - amd_iommu=off
          - amd_pstate=active
          - amdgpu.gttsize=126976
          - amdgpu.vm_fragment_size=8
          - ttm.pages_limit=32505856
          - ttm.page_pool_size=25165824
        systemExtensions:
          officialExtensions:
            - siderolabs/amdgpu
            - siderolabs/amd-ucode
    machineSpec:
      secureboot: false
    kernelModules:
      - name: amdgpu
        parameters: ["ppfeaturemask=0xffffffff"]
    userVolumes:
      - name: local-models
        provisioning:
          diskSelector:
            match: disk.model == "<data-nvme-model>" && !system_disk
          minSize: 500GB
    networkInterfaces:
      - # single 5GbE — no bond, no VIP (worker)
        deviceSelector:
          hardwareAddr: "<framework-mac>:*"
        dhcp: false
        addresses: ["10.10.40.25/24"]
        routes:
          - { network: "0.0.0.0/0", gateway: "10.10.40.1" }
        mtu: 1500
        vlans:
          - { vlanId: 20, dhcp: false, mtu: 1500 }
          - { vlanId: 90, dhcp: false, mtu: 1500 }
    nodeLabels:
      topology.kubernetes.io/gpus: amd
    nodeTaints:
      llm-workload: "true:NoSchedule"
    patches:
      - "@./patches/framework/oom.yaml"
```

**`talos/patches/framework/oom.yaml`** (OOMConfig has no native talhelper field, so
it rides in via node `patches`; the amdgpu GTT pins unreclaimable RAM on a 128 GB UMA
node, so without this, stacked GPU pods can deadlock the node):

```yaml
apiVersion: v1alpha1
kind: OOMConfig
triggerExpression: 'memory_full_avg10 > 12.0 && d_memory_full_avg10 > 0.0 && time_since_trigger > duration("500ms")'
cgroupRankingExpression: '{Besteffort: 1.0, Burstable: 0.5, Guaranteed: 0.0, Podruntime: 0.0, System: 0.0}[class] * double(memory_current.orValue(0u))'
sampleInterval: 100ms
```

### ⚠️ Tooling gotcha: `talosImageURL` vs inline `schematic`

The `.taskfiles/talos` **`upgrade-node`** task reads the image via
`yq '.nodes[] | select(.ipAddress==IP) | .talosImageURL'`. A node using inline
`schematic:` has **no `talosImageURL`**, so that task would pass a broken
`--image=':v1.13.x'`. Pick one:

- **Keep parity:** also set `talosImageURL` on the framework node (build the AMD
  schematic once at factory.talos.dev, paste the URL). One manual step, existing
  tasks unchanged.
- **Go inline (cleaner config):** add a yq fallback in `upgrade-node` so it uses
  talhelper's schematic-derived image when `talosImageURL` is absent.

## Bootstrap-path fix — 2.5G management interface (chosen approach)

**Scope decision:** do **not** re-architect the service/Ceph networks. Everything on
`bond0` / `10.10.40.0/24` (Ceph host-net public+cluster, Cilium native routing, node IP,
VIP, LB pool, VLANs 20/90) stays **exactly as-is**. Only fix the Talos bootstrap pain.

**Problem.** In maintenance mode the LACP `bond0` isn't up, and UniFi has no LACP
fallback, so a fresh node is unreachable on the 10G ports to apply config.

**Fix.** Light up the 2× 2.5G `igc` ports (currently `ignore: true`) as a **dedicated
management/bootstrap interface** on the existing `10.10.5.0/24` Management VLAN:
- It is **not** the node IP, **not** the VIP, and carries **no default route** — purely
  an out-of-band Talos API/apply path.
- Maintenance mode: the node DHCPs on the 2.5G (plain access port) → reachable at a
  `10.10.5.x` lease → `apply-config` over it. The 10G LAG need not pass traffic yet.
- Bonus: an OOB lifeline — a bad `bond0` config no longer locks you out; reach the node
  on `10.10.5` to roll back.

Nothing else moves: no renumber, no Rook change, no cert/VIP/LB change.

### talconfig change (replaces the current `ignore: true` igc block)

```yaml
      - # 2.5G management/bootstrap on VLAN 5 — independent of the 10G LACP bond
        interface: bond1
        bond:
          deviceSelectors: [{ hardwareAddr: "58:47:ca:76:*", driver: igc }]
          mode: active-backup        # host-side only; NO switch LAG
          miimon: 100
        dhcp: false
        addresses: ["10.10.5.21/24"]  # .22/.23/.24 on the others
        mtu: 1500
        # deliberately NO routes — default stays via bond0 → 10.10.40.1
```

The 10G `bond0` block is unchanged. Point talosctl **endpoints** at the `10.10.5.x`
addresses so management/apply uses the stable OOB path; the k8s API VIP
`10.10.40.20` and `nodeIP.validSubnets: 10.10.40.0/24` are untouched.

### UniFi side
- **No static IPs to assign** — Talos sets `10.10.5.x` from talconfig.
- **Keep DHCP on the `10.10.5` Management network** — that's what makes a maintenance-mode
  node reachable to apply. (Optional reservations for predictable maintenance IPs.)
- **2.5G ports → plain access ports on the Management VLAN. No switch LAG** (active-backup
  is host-side; a LAG would break it).
- **10G ports + `10.10.40` + BGP: leave completely untouched.**
- Bootstrap/manage from a host on (or routed to) the Management VLAN, since the `10.10.5`
  interface has no default route.
- **vPro/AMT:** if you want a dedicated OOB port, select a single igc MAC here and leave
  the other `ignore: true` instead of bonding both.

### Applying to the live cluster (no rebuild required)

This change is **additive** and safe to apply to the running cluster, with one
precaution already handled in the repo:

- **Cilium `devices`** was changed `bond+` → `bond0+`
  (`kubernetes/apps/kube-system/cilium/app/helmrelease.yaml`). `bond+` would have made
  Cilium attach its BPF datapath to the new `bond1`; `bond0+` keeps `bond0` + its VLANs
  and excludes `bond1`. It matches the same interfaces as today until `bond1` exists, so
  merging it is a no-op now. **Merge/reconcile this first**, before applying `bond1`.
- `bond0` is unchanged; `bond1` has **no node IP, no VIP, no default route**, so routing,
  Cilium, BGP and Ceph (host-net on `10.10.40`) are untouched.
- Apply **one node at a time** with **`--mode=try`** (auto-reverts on a timeout if
  anything misbehaves): `talhelper gencommand apply --node <ip> --extra-flags '--mode=try' | bash`.
- Adding an interface is a live, no-reboot change. Order vs UniFi doesn't affect safety:
  until the 2.5G ports are on the Management VLAN, `bond1` is simply inert.

> ⚠️ **2026-06-11 — that "clean additive apply" turned out to be blocked.** A dry-run
> against the live cluster revealed significant pre-existing drift, so `bond1` cannot be
> applied in isolation via the full machineconfig. See the next section. The Cilium
> `bond0+` change *was* applied (via Flux) and is fine; the `bond1` talconfig change is
> committed but **not yet applied to any node**.

## Config-drift reconciliation & rollout (REQUIRED before bond1 can land)

A `talosctl apply-config --dry-run` on talos-1 showed the running nodes have drifted from
what `talhelper genconfig` (v3.1.10) now produces:

| Drift | Running cluster | New generated | Disposition |
|---|---|---|---|
| **Network format** | inline `machine.network.interfaces` | multi-doc (`BondConfig`/`LinkAliasConfig`/`VLANConfig`/`Layer2VIPConfig`) | **Accept** — talhelper 3.x moved to multi-doc; end-state network is equivalent (bond0 + VLANs + VIP + new bond1). Cleanest applied via reboot (fresh boot builds new format). |
| **Install image** | `…:v1.13.3` | `…:v1.13.2` | **Accept** — fallout from the git installer revert. `apply-config` only updates the stored ref; it does **not** upgrade/downgrade the running OS (already v1.13.2). |
| **Features** | `rbac/stableHostname/apidCheckExtKeyUsage: true` explicit | omitted | **Accept** — default-on in modern Talos, so a no-op. Verify on the worker; optionally pin via a global patch for zero ambiguity. |
| **certSANs / grub** | — | `grubUseUKICmdline: true`, certSANs shuffle | Benign. |

Because the `features` change isn't live-applicable, `--mode=try` is rejected and
`--mode=auto` **reboots** each node. So this is a planned, reboot-bearing rollout — not a
live tweak. **Decision (2026-06-11): do the full reconciliation, validate on the worker
first.** `bond1` rides along for free.

### Pre-flight
1. Confirm intended Talos = **v1.13.2** (git revert). Do **not** bundle a Talos upgrade.
2. `talosctl -n <ip> apply-config --file clusterconfig/kubernetes-<node>.yaml --mode=auto --dry-run`
   on **all four** nodes; confirm each diff matches the table above (no surprises).
3. Backups: per node `talosctl -n <ip> get mc v1alpha1 -o yaml > backups/<node>.yaml`;
   etcd snapshot `talosctl -n <cp> etcd snapshot etcd-$(date +%F).db`; copy current `talosconfig`.
4. Ceph: `ceph osd set noout` (skip rebalance during brief reboots); confirm `HEALTH_OK`.
5. Confirm UniFi 2.5G ports are on the Management VLAN with DHCP (done 2026-06-11).

### Phase 1 — worker `talos-4` (proves the migration, low blast radius)
- `kubectl cordon talos-4 && kubectl drain talos-4 --ignore-daemonsets --delete-emptydir-data`
- `task talos:apply-node IP=10.10.40.24 MODE=auto` (reboots)
- Validate: node `Ready`; `talosctl -n 10.10.40.24 get links,addresses` shows `bond0`
  (`10.10.40.24` + VLANs) **and** `bond1` (`10.10.5.24`); `features` still active; Ceph OSDs
  rejoin; **reach the Talos API on `10.10.5.24`** (OOB path works).
- `kubectl uncordon talos-4`. **Gate:** proceed only if fully clean.

### Phase 2 — control plane `talos-1/2/3`, ONE at a time
Per node: verify `etcd` healthy + Ceph OK → cordon/drain → `task talos:apply-node IP=<ip> MODE=auto`
→ wait `Ready` + etcd member healthy + VIP `10.10.40.20` reachable → uncordon → validate → next.
Never have more than one control-plane node down (3-node quorum).

### Phase 3 — finalize
- `ceph osd unset noout`.
- Optionally repoint talosctl endpoints to the OOB path: `talosctl config endpoint 10.10.5.21 10.10.5.22 10.10.5.23`.
- Verify `bond1`/OOB on all nodes; `kubectl get nodes` all `Ready`; Ceph `HEALTH_OK`; apps healthy.

### Rollback
Per node: reapply the backup (`talosctl apply-config --file backups/<node>.yaml --mode=auto`)
or `talosctl rollback`. etcd snapshot covers worst case. Worker-first ordering means a bad
migration is caught on `talos-4` before any control-plane node is at risk.

## Open decisions before writing YAML

- First model + PVC sizing / decode args (e.g. a Qwen3 ~32B-class for the 128 GB box).
- `ceph-block` per-model vs one shared `ceph-filesystem` RWX cache.
- Framework IP/hostname/VLANs (placeholder: `10.10.40.25` / `framework`).
- Do the 3090 (phase 6) now or stub for later.

## Reference patterns (joryirving/home-ops)

- Strix worker: `talos/main/worker/{schematic.yaml,skirk.yaml}` (incl. OOMConfig).
- eGPU NVIDIA node: `talos/main/controlplane/{eGPU-schematic.yaml,eGPU.yaml}`.
- LLM apps: `kubernetes/apps/base/llm/{litellm,open-webui,toolhive}`.
- Device plugins: `kubernetes/apps/base/kube-system/{amd,nvidia}-device-plugin`.
