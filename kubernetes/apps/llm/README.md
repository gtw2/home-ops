# llm

Self-hosted inference on the Framework Desktop (`framework`, Ryzen AI Max /
Strix Halo, 128GB unified memory), fronted by an OpenAI-compatible proxy.
Modelled on [joryirving/home-ops](https://github.com/joryirving/home-ops/tree/main/kubernetes/apps/base/llm),
trimmed to the parts this cluster can actually run today.

```
llmkube/            # operator: Model + InferenceService CRDs, shared model cache,
                    # priority classes, the AMD DRA ResourceClaimTemplate
litellm-operator/   # operator: LiteLLMProxy / LiteLLMModel / LiteLLMVirtualKey CRDs
litellm/app/        # the proxy, its database, and the models it serves
  llama-strix.yaml    #   Model + InferenceService (llama.cpp on the Framework)
  models/             #   LiteLLMModel: how the proxy addresses that backend
  virtualkey.yaml     #   per-consumer API key, pushed to 1Password
```

The split follows the reference: `llmkube/` holds only the operator and
cluster-wide GPU plumbing, and each model's two CRs live in the folder of the
app that consumes them, reconciled by that app's own Kustomization.

## How a request flows

`client -> litellm.${SECRET_DOMAIN} (envoy-external) -> LiteLLMProxy -> llama-strix InferenceService -> Strix Halo iGPU (DRA)`

The GPU is claimed through DRA: the `framework-gpu` ResourceClaimTemplate
requests the `gpu.amd.com` DeviceClass published by the ROCm
`k8s-gpu-dra-driver` DaemonSet in `kube-system`.

## Before the first reconcile

1. **1Password items.** A `litellm` item with `LITELLM_MASTER_KEY` (an `sk-…`
   string), `LITELLM_SALT_KEY`, `LITELLM_CLIENT_ID` / `LITELLM_CLIENT_SECRET`
   (Authentik), and `LITELLM_POSTGRES_DB` / `_USER` / `_PASSWORD`. Nothing
   else — the scaffolded model is a public repo, so no HuggingFace token is
   needed until a gated one is added.
2. **Authentik application** for litellm: redirect URI
   `https://litellm.${SECRET_DOMAIN}/sso/callback`, and a `litellm_role`
   property mapping (`internal_user` / `proxy_admin`).
3. **The GGUF filename** in `litellm/app/llama-strix.yaml` was verified against
   the repo on 2026-09-07 (~16.5GiB). Re-check it if this sits unapplied for a
   while — unsloth renames quant files between uploads, and a wrong name fails
   at download time, not at apply time.

## Prerequisites that live outside this directory

The Framework is dedicated by `llm-workload=true:NoSchedule`, which also keeps
cluster infrastructure off it unless that infrastructure tolerates the taint:

- `ceph-csi-drivers` — `drivers.{cephfs,rbd}.nodePlugin.tolerations` (added
  with this scaffold). Without it the CSI node plugin never runs on the
  Framework, so no PVC mounts there at all, including the model cache.
- `dragonfly` — the `llm` namespace was added to the cross-namespace
  NetworkPolicy allowlist so the proxy can reach redis.
- Still not tolerating the taint, and out of scope here: spegel (no P2P image
  pulls on that node), node-exporter and the rest of the observability
  DaemonSets (no node metrics from the Framework).

The operator's shared cache is CephFS RWX, since `shared` mode needs one PVC
reachable from anywhere. Weights would therefore stream over the network at
every pod start, so `llama-strix` overrides it with `modelCache.claimName`
pointing at a 500Gi `openebs-hostpath` claim on the Framework's own NVMe
(`nvme1n1p1`, 4.1TB). openebs-hostpath provisions fine on the tainted node: the
localpv helper pod copies the target node's taints into its own tolerations.

## Adding a model

Drop a `Model` + `InferenceService` pair into `litellm/app/` and add it to that
kustomization. `litellm-operator`'s `llmkube.autoRegister` picks it up under the
InferenceService name automatically; add a `LiteLLMModel` under
`litellm/app/models/` only to expose a tuned alias with its own sampling
defaults and token limits.

GPU access: reference `framework-gpu` as the model's
`resourceClaimTemplateName`. There is one GPU in this cluster, so there is one
template.

## Known follow-ups

- **open-webui** now talks to this proxy from `home-automation`
  (`OPENAI_API_BASE_URL`, key from `virtualkey.yaml` via 1Password). It has to
  reconcile *after* the virtual key exists in 1Password, or its ExternalSecret
  has nothing to template. Moving the app into this namespace would mean
  migrating its PVC and Kopiur backup, so it stays where it is.
- The operator-generated HTTPRoute takes no annotations, so litellm has no
  homepage or gatus entry yet — both would need a hand-written route or a
  homepage config entry.
- No `PrometheusRule` or Grafana dashboards yet; the chart ships dashboards
  (`grafana.dashboards.enabled`) if wanted.
