# Talos configuration

Machine configs are rendered from a Jinja template by minijinja. There is no
talhelper in this path.

```
talos/
  machineconfig.yaml.j2   # the whole config, both roles
  cluster.yaml            # cluster-wide values (versions, subnets, schematic)
  nodes/talos-N.yaml      # per-node values — 4 scalars plus role
  talsecret.sops.yaml     # PKI + tokens, SOPS/age encrypted
  nut.sops.yaml           # NUT client password, SOPS/age encrypted
```

Context is merged from those four sources at render time. Secrets are piped
straight from `sops` into minijinja and never written to disk.

## Usage

```sh
task talos:render     NODE=talos-1     # print the rendered config
task talos:validate                    # render + talosctl validate, all nodes
task talos:diff-node  NODE=talos-1     # dry-run against the live node
task talos:apply-node NODE=talos-1     # apply (MODE=auto by default)
task talos:upgrade-node NODE=talos-1   # Talos upgrade, version + schematic from cluster.yaml
```

`talos:diff-node` before `talos:apply-node`, always. An empty diff means the
node already matches the repo.

## Why talhelper was dropped

talhelper switches its output format at `talosVersion` 1.14+, emitting the
multi-document config instead of v1alpha1. Two problems followed.

**It cannot round-trip an existing cluster's etcd encryption.** talhelper
generates `KubeEtcdEncryptionConfig` with the secretbox key named `key1` and no
`identity` provider. Talos, deriving the same secret from v1alpha1
`secretboxEncryptionSecret`, names it `key2` and adds an `identity` fallback.
Secretbox resolves keys by name, so every secret this cluster has ever written
becomes undecryptable — kube-apiserver fails readiness with `no matching key was
found for the provided Secretbox transformer`.

**That document cannot be overridden.** A patch setting its name, providers and
resources to distinctive probe values has no effect at all, while patches to
`KubeAPIServerConfig`, `KubeProxyConfig` and `KubeAdmissionControlConfig` in the
same file apply normally. talhelper regenerates it from `talsecret` after
patches run.

Pinning `talosVersion` back to 1.13.10 kept v1alpha1 output, but talhelper then
refuses the pairing outright once Kubernetes moves ahead:

```
version of Kubernetes 1.37.0 is too new to be used with Talos 1.13.10
```

which left the repo unable to generate any config at all.

A template has no version-gated behaviour and no authority over its own output,
so neither failure is reachable. The per-node cost is low: the bond, VLAN, VIP
and volume topology is identical on every node, and only four scalars vary —
disk serial, hostname, and the two bond addresses.

Worth knowing: `HostnameConfig.auto` is deliberately omitted. It conflicts with
a static `hostname`, and Talos serialises the unset enum back out as
`auto: false`, which it will not accept as input — so a config read off a live
node is not directly re-appliable without dropping that field.

## Bootstrap

```sh
task bootstrap:talos-secrets   # once, ever — generates talsecret.sops.yaml
task bootstrap:talosconfig     # generates clusterconfig/talosconfig
task bootstrap:talos           # both of the above, then apply + bootstrap
```

`talosctl gen secrets` emits exactly the shape `machineconfig.yaml.j2` consumes
(`cluster.*`, `secrets.*`, `trustdinfo.*`, `certs.*`) — the same bundle format
talhelper used, so no conversion is involved.

`talos-secrets` is guarded by a `status:` check and will not overwrite an
existing `talsecret.sops.yaml`. Regenerating it against a live cluster would
mint a new PKI and orphan every node and every encrypted secret.

`talosconfig` is safe to re-run at any time: it reissues the admin client
certificate from the same CA, so previously issued configs keep working. The
endpoints are derived from whichever `nodes/*.yaml` have `controlPlane: true`.

Note both tasks pass secrets via process substitution as an *argument*
(`--with-secrets <(sops -d ...)`). The `< <(...)` stdin-redirect form hangs
under task's shell interpreter.

## Hardware profiles

Nodes select a `profile` in `nodes/<node>.yaml`, which picks the Image Factory
schematic and the network topology:

| profile  | hardware                        | network                                    |
| -------- | ------------------------------- | ------------------------------------------ |
| `intel`  | Intel NUC-class, dual NIC       | 10G LACP `bond0` + 2.5G mgmt `bond1` (VLAN 5), VLANs 20/90, MTU 9000 |
| `amd-ai` | Framework Desktop, Strix Halo   | single onboard NIC, one-link `bond0`, MTU 1500 |

Kernel cmdline args live in the **schematic**, not machineconfig — Talos 1.14's
multi-document config covers sysctl/sysfs/modules only. Changing a kernel arg
mints a new schematic id and needs a Talos upgrade to take effect.

## Adding the AI worker

`framework` is a Framework Desktop (Ryzen AI Max, 128GB unified memory) dedicated
to AI workloads. Three values in `nodes/framework.yaml` must come off the real
hardware before it can be applied — boot it in maintenance mode and read them:

```sh
talosctl -n <maintenance-ip> get disks   # -> installDiskSerial
talosctl -n <maintenance-ip> get links   # -> primaryNic (permanentAddr of the onboard NIC)
```

The third is `address`, currently scaffolded as `10.10.40.25/24`.

Notes on the profile:

- **Dedicated by taint.** `llm-workload=true:NoSchedule`, so only tolerating
  pods land here and the GTT-pinned memory is not contended. Labels are
  `topology.kubernetes.io/gpus: amd` (the workload selector) and
  `node-role.kubernetes.io/rocm-worker: "true"`, matching the reference Strix
  Halo node. The backend choice lives in the llama.cpp container image, not
  here — the `amdgpu` extension provides both `/dev/dri` and `/dev/kfd`, so this
  node config supports Vulkan or ROCm without change.
- **MTU 9000, not 1500.** Ceph's `public_network` and `cluster_network` are both
  `10.10.40.0/24` and every other node runs `bond0` at 9000. A 1500-MTU host on
  that L2 segment has no router in the path to fragment or signal "packet too
  big", so large OSD reads black-hole while small ops keep working. Confirm the
  onboard NIC does jumbo frames.
- **Node labels must sit under `node.kubernetes.io/`.** The NodeRestriction
  admission plugin only lets a kubelet self-set labels from an allowlist:
  `node-role.kubernetes.io/*` is blocked outright and `topology.kubernetes.io/`
  permits only `region`/`zone`. Labels outside that allowlist fail silently from
  the cluster's point of view — Talos retries the patch every ~15s forever and
  `kubectl get node` simply never shows them. `node.kubernetes.io/*` is allowed,
  which is why the Intel nodes' `node.kubernetes.io/gpu` works.
- **Memory split is a BIOS decision, not a kernel-arg one.** The BIOS setting
  is **Advanced → iGPU Memory Configuration → Custom → 512MiB** (since
  2026-10-14), and it is not in this repo, so a BIOS reset or board swap
  silently undoes it. Enter setup with F2 from a cold power-on;
  `talosctl reboot` is too quick to catch. With it, `mem_info_vram_total`
  reads 0.5GiB, `mem_info_gtt_total` 124GiB and node allocatable 122.6GiB, and
  the GPU allocates GTT dynamically from the ~124GiB system pool up to
  `ttm.pages_limit=32505856` (124GiB).

  It shipped as a 96GiB carve-out, which left the OS ~31GiB and capped GTT at
  that too. `qwen3.8-flash-next-gufo.yaml` cannot fit under that (its weights
  exceed 96GiB) and the llama.cpp option could not afford a second slot.

  The cost: kubelet counts GTT as free memory, because hipMalloc'd GTT is not
  charged to any cgroup, so allocatable overstates what is left by however
  much the model holds (~89-95GiB). The `llm-workload` taint keeps other pods
  off, and the `OOMConfig` below sees GTT pressure where kubelet does not.
  `amdgpu.gttsize` also now warns as deprecated in favour of
  `ttm.pages_limit`.
- **The `OOMConfig` is not optional.** amdgpu GTT pins system RAM the kernel
  OOM-killer cannot reclaim, so a runaway GPU pod deadlocks the node while Talos
  stays healthy — which means the hardware watchdog never fires. The userspace
  OOM manager kills the heaviest user pod under sustained memory PSI pressure
  instead.
- **Storage consumer only, but model weights are local.** No Ceph OSD — the
  CephCluster `devicePathFilter` targets Samsung MZQL2 drives. Model weights do
  live on a local `local-hostpath` volume on the second NVMe rather than the
  `ceph-block` PVC the plan doc originally specified: a cold GGUF load off Ceph
  is a multi-minute network read (~5.7 min for a 40GB model on 1GbE, ~70s on
  5GbE), and on a tainted single-GPU node the pod is pinned here anyway, so
  ceph-block's "survives reschedule" argument buys little.
- **`localHostpathMatch` is a full CEL expression**, single-quoted on render. An
  expression beginning with `!` is a YAML *tag* if emitted bare — talosctl
  accepts it, other parsers reject it, and they do not agree on the value.

Still to do at the Kubernetes layer, separate from machineconfig: the AMD GPU
device plugin (or GPU Operator with driver installation disabled), which
advertises `amd.com/gpu` and exposes `/dev/dri` + `/dev/kfd` to pods.

## Changing a schematic

Extensions and kernel args live in the Image Factory schematic, so adding one
means a new id and a reinstall of each node at the same Talos version.

1. Fetch the current definition, edit it, and POST it back for the new id (the
   comment above `schematics` in `cluster.yaml` has the URLs). Put the id in
   `cluster.yaml`.
2. Per node: `task talos:diff-node`, `task talos:apply-node`, then
   `task talos:upgrade-node`. Wait for Ceph to return to `HEALTH_OK` before
   the next node. Nothing gates that for you on this path.

`apply-node` only records the new installer image; `upgrade-node` is what
installs it. tuppr will not do the second step, because it compares versions
and not schematics. Between the two steps tuppr (0.5.13) refuses to upgrade
that node at all, with `install image ... does not embed the runtime
schematic`, so do not merge a Talos version bump while nodes are half-way.

## UPS shutdown

Every node runs the `nut-client` extension: NUT's `upsmon`, logged in to the
NUT server on tank as a secondary. When tank declares a forced shutdown,
`upsmon` runs `/sbin/poweroff`. That is Talos's own init binary, bind-mounted
into the extension, and it asks machined over the node's local socket for an
ordinary graceful shutdown. Nothing holds a Talos API credential, and a node
needs only tank in order to act: not the Kubernetes API, not DNS, not another
node.

The config is the `ExtensionServiceConfig` in `machineconfig.yaml.j2`, fed by
`nutMonitor` and `nutUser` in `cluster.yaml` and `nutPassword` in
`nut.sops.yaml`. Create that file once, from the repo root:

```sh
printf 'nutPassword: %s\n' "$(openssl rand -hex 16)" |
  sops --filename-override talos/nut.sops.yaml --encrypt /dev/stdin \
  > talos/nut.sops.yaml
```

No `talos:` task will render until it exists. tank needs the matching user, in
the TrueNAS UPS service's Extra Users:

```
[talos]
  password = <nutPassword>
  upsmon secondary
```

What decides the timing:

- **When the nodes go is tank's decision alone.** upsmon on a node cannot run
  timers of its own (the extension has no shell, so no `NOTIFYCMD` or
  `upssched`). The nodes go when tank sets the forced-shutdown flag, which its
  Shutdown Mode and Shutdown Timer control.
- **How long a node takes.** `/sbin/poweroff` takes no arguments, so this is
  always the non-forced path: cordon, drain, then stop pods. With every node
  draining at once the PodDisruptionBudgets cannot be satisfied (the CNPG
  primaries allow 0 disruptions, Ceph allows 1), and those evictions retry
  until Talos's fixed 5 minute drain timeout. Read from the 1.14.5 source and
  not yet measured: expect 6-7 minutes from the signal on the Intel nodes, and
  a minute or two on `framework`, which runs nothing behind a budget.
- **What tank waits for.** `SHUTDOWNEXIT no` keeps each node's upsmon logged
  in until Talos stops the service, after the pods are gone. tank's Host Sync
  is how long it waits for those logins to drop before shutting down anyway.
  It has to be longer than the figure above, or NFS disappears under pods that
  are still stopping.

Checks:

```sh
talosctl -n <ip> service ext-nut-client   # STATE Running
talosctl -n <ip> logs ext-nut-client      # no "access denied"
# Logins on tank: 1 for tank itself + 1 per node. bash only, no nc needed.
bash -c 'exec 3<>/dev/tcp/tank.internal/3493; printf "GET NUMLOGINS ups-main\n" >&3; head -1 <&3'
```

`upsmon -c fsd` on tank is the end-to-end test, and it is not a dry run: it
powers off every node and tank itself.

## Open items

- **talhelper bug.** `KubeEtcdEncryptionConfig` should preserve Talos's `key2`
  naming and `identity` provider for pre-1.14 clusters, and should be patchable.
  Not yet filed upstream.
- **Anonymous auth.** talhelper's `KubeAuthenticationConfig` enables anonymous
  access to the health endpoints; this cluster runs `--anonymous-auth=false`.
  Moot now, but it was an unrequested change worth remembering.
