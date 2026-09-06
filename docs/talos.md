# Talos configuration

Machine configs are rendered from a Jinja template by minijinja. There is no
talhelper in this path.

```
talos/
  machineconfig.yaml.j2   # the whole config, both roles
  cluster.yaml            # cluster-wide values (versions, subnets, schematic)
  nodes/talos-N.yaml      # per-node values — 4 scalars plus role
  talsecret.sops.yaml     # PKI + tokens, SOPS/age encrypted
```

Context is merged from those three sources at render time. Secrets are piped
straight from `sops` into minijinja and never written to disk.

## Usage

```sh
task talos:render     NODE=talos-1     # print the rendered config
task talos:validate                    # render + talosctl validate, all nodes
task talos:diff-node  NODE=talos-1     # dry-run against the live node
task talos:apply-node NODE=talos-1     # apply (MODE=auto by default)
task talos:upgrade-node NODE=talos-1   # Talos upgrade, version from cluster.yaml
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

## Open items

- **Bootstrap from scratch.** `task bootstrap:talos` assumes
  `talsecret.sops.yaml` and `clusterconfig/talosconfig` already exist. Both were
  originally produced by `talhelper gensecret`/`genconfig`; regenerating them
  needs `talosctl gen secrets` plus a shape conversion, which has not been
  written or tested.
- **talhelper bug.** `KubeEtcdEncryptionConfig` should preserve Talos's `key2`
  naming and `identity` provider for pre-1.14 clusters, and should be patchable.
  Not yet filed upstream.
- **Anonymous auth.** talhelper's `KubeAuthenticationConfig` enables anonymous
  access to the health endpoints; this cluster runs `--anonymous-auth=false`.
  Moot now, but it was an unrequested change worth remembering.
