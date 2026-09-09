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
toolhive/           # MCP tooling: the operator, the servers, and the gateway
  crds/ app/          #   operator install, split so CRDs land first
  config/             #   MCPGroup, embedding model, VirtualMCPServer, route
  mcp-servers/        #   one directory per MCP server (kubectl, flux)
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

## Tools (MCP)

The model can read the cluster itself rather than waiting for someone to paste a
manifest into chat. `toolhive/` runs each MCP server as a pod and aggregates them
behind one endpoint.

```
open-webui -> vmcp-mcp-gateway.llm:4483 -> { kubectl-mcp, flux-mcp } -> apiserver
```

Both servers are **read-only**, enforced by RBAC rather than by a flag:
`kubectl-mcp-readonly` (aggregation, tracking the built-in `view` role) plus
`kubectl-mcp-readonly-explicit` (everything aggregation misses). Kubernetes
Secrets are excluded from both, as are exec/attach/proxy subresources. `flux-mcp`
binds those same two roles and nothing else, so it can explain why a
Kustomization is failing but cannot suspend, patch or delete one. The upstream
reference also ships a `flux-mcp-write` role; adding it is a deliberate,
separate commit, not an edit to `mcp-servers/flux/rbac.yaml`.

The explicit role names every non-core apiGroup **in this cluster**, generated
from `kubectl api-resources` rather than copied. Regenerate it when a new
operator adds a CRD group, unless that operator ships an
`aggregate-to-view` ClusterRole, in which case the aggregation picks it up for
free.

Tool selection is semantic, and it works by indirection rather than by
filtering `tools/list`. With the optimizer on, the gateway advertises exactly
**two** tools - `find_tool` and `call_tool` - regardless of how many backends
join. The model describes what it wants (`tool_description` plus
`tool_keywords`), gets back the closest `maxToolsToReturn` (8) matches by hybrid
semantic/keyword search, then invokes one through `call_tool`. So the context
cost of adding a backend is zero; the cost is one extra round trip per task.
Turning the optimizer off would expose every backend tool directly instead. The embeddings
come from `toolhive-embed`, a CPU llama.cpp InferenceService on the talos nodes —
it carries no `llm-workload` toleration, so the Framework stays dedicated to
`llama-strix`. `embeddingProvider: openai` names a **wire protocol**, not a
vendor; no key is set, and per the CRD an empty key omits the Authorization
header, which is what a keyless in-cluster endpoint wants.

### Before the first reconcile of toolhive

**Create the 1Password `toolhive` item first**, with `MCP_GATEWAY_API_KEY` set to
a random string. This is not just a convenience: if the ExternalSecret cannot
produce `mcp-gateway-api-keys`, Envoy reports the SecurityPolicy as invalid and
does not enforce it, which would leave `mcp.${SECRET_DOMAIN}` answering
cluster-read queries to anything on the LAN. After reconcile, confirm the policy
actually attached before trusting the route:

```sh
curl -so /dev/null -w '%{http_code}\n' https://mcp.${SECRET_DOMAIN}/mcp   # expect 401
kubectl get securitypolicy -n llm mcp-gateway-api-key -o jsonpath='{.status.conditions}'
```

The route is on `envoy-internal`, not `envoy-external` — this endpoint can read
every non-secret object in the cluster. Promoting it is a one-line `parentRefs`
change, and the API key stays either way.

### Wiring open-webui to it

Add the tool server in **Settings -> Admin -> Integrations -> External Tool
Servers**, type *MCP (Streamable HTTP)*, URL
`http://vmcp-mcp-gateway.llm.svc.cluster.local:4483/mcp`. In-cluster, so it
bypasses envoy and needs no API key.

Do **not** set `TOOL_SERVER_CONNECTIONS` in the HelmRelease. It is a
PersistentConfig variable: on an instance whose database already exists, the env
var is read once at first boot and ignored forever after, so the change appears
to apply and silently does nothing.

### Adding another MCP server

Create `toolhive/mcp-servers/<name>/` with an `MCPServer` (or `MCPServerEntry`
for something already running elsewhere), `groupRef: mcp-tools`, and its own
ServiceAccount if it needs cluster access. Add a Kustomization to
`toolhive/ks.yaml` depending on `toolhive-config`, which owns the MCPGroup. The
gateway discovers new backends at runtime; tool-name collisions are resolved by
the `{workload}_` prefix.

Everything is written against the `v1beta1` toolhive APIs. `v1alpha1` is still
served but deprecated, and `v1beta1` is the storage version for every kind here.

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
