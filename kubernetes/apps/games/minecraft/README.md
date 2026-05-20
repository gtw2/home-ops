# Minecraft

```
                                              ┌─────────────────┐
   Player → mc.${SECRET_DOMAIN}:25565 ───────►│  Velocity       │ public LB 10.10.40.150
                                              │  (mc-proxy)     │ TCP + UDP voice/bedrock
                                              └────────┬────────┘
                                                       │ /server X or forced-host route →
                                                       │ dials <server>.games.svc.cluster.local
                                                       │ (ExternalName CNAME to mc-router)
                                                       ▼
                                              ┌─────────────────┐
                                              │  mc-router      │ ClusterIP
                                              │  (in-cluster)   │ scales the backing STS
                                              └────────┬────────┘ on connect, then relays
                                                       │ Velocity's bytes verbatim
                                                       ▼
                            ┌──────────────┬───────────┴───────────┬──────────────┐
                            ▼              ▼                       ▼              ▼
                       ┌─────────┐   ┌─────────┐            ┌──────────────┐ ┌──────────────┐
                       │ vanilla │   │ create  │            │ vanilla-     │ │ create-      │
                       │ STS 0   │   │ STS 0   │            │ creative     │ │ creative     │
                       └─────────┘   └─────────┘            │ STS 0        │ │ STS 0        │
                                                            └──────────────┘ └──────────────┘

                       Hub (lobby) is always-on; Velocity dials it directly via cluster DNS.
                       mc-router is byte-faithful, so Velocity's modern player-info-forwarding
                       handshake reaches the backend intact through the proxy.
```

## Roles

| Component | State | Owns |
|---|---|---|
| **Velocity** (`velocity/`) | Always-on, public LB `10.10.40.150` (TCP + UDP) | Public entrypoint, LuckPerms permissions, `/server` hopping, voicechat proxy, MiniMOTD, tab list, forced-host subdomains, modern player-info-forwarding to backends |
| **mc-router** (`router/`) | Always-on, ClusterIP | Sits between Velocity and the scalable backends. Reads Velocity's handshake hostname, scales the matching StatefulSet 0→1, waits for it to be reachable, then relays bytes through |
| **Lobby** (`lobby/`) | Always-on | Velocity's `try` fallback; the landing server |
| **Vanilla / Create / *-Creative** | `replicas: 0` | World data; woken whenever Velocity dials their `*.games.svc.cluster.local` alias |

## Auto-scaling behavior

- mc-router scales a backend StatefulSet **0 → 1** when Velocity opens a TCP connection whose handshake hostname matches the Service's `externalServerName` annotation. (mc-router only supports StatefulSets — backends must use `controllers.<name>.type: statefulset` with `statefulset.serviceName` set equal to the controller name.)
- The scale-up waker polls the backend's TCP endpoint until it answers, then forwards. Per-backend timeout is set via `mc-router.itzg.me/autoScaleWaitTimeout: "3m"` to cover modded cold starts.
- After **30 minutes** of no active connections, mc-router scales the backend back to **0**.
- The 30m timeout is global — mc-router doesn't support per-service overrides. Tune in `router/helmrelease.yaml` (`--auto-scale-down-after`); today survival and creative share it.
- **`/server X` triggers scale-up:** because Velocity's `[servers]` entries point at the ExternalName aliases, every `/server Vanilla` (or forced-host route) goes through mc-router, which scales the STS if it's at zero. No special "wake me first" hostname needed — direct connection via `vanilla.${CFG_GAMING_DOMAIN}` and in-game `/server Vanilla` both Just Work.

## How a backend gets routed through mc-router

Every scaled backend needs **three** things:

1. **The HelmRelease** (`<server>/helmrelease.yaml`): backend is a StatefulSet with `replicas: 0`, and its Service carries:
   ```yaml
   annotations:
     mc-router.itzg.me/externalServerName: "<server>.games.svc.cluster.local"
     mc-router.itzg.me/autoScaleWaitTimeout: "3m"
   ```

2. **An ExternalName Service alias** (`<server>/route-services.yaml`) — so Velocity can DNS-resolve `<server>.games.svc.cluster.local` to mc-router:
   ```yaml
   apiVersion: v1
   kind: Service
   metadata: { name: <server> }
   spec:
     type: ExternalName
     externalName: minecraft-router.games.svc.cluster.local
   ```

3. **A Velocity backend entry** (`velocity/config/velocity.toml`):
   ```toml
   [servers]
   <Name> = "<server>.games.svc.cluster.local:25565"
   ```

A matching `[forced-hosts]` entry routes players who connect via a public subdomain (`<server>.${CFG_GAMING_DOMAIN}`) into that backend, hitting the same scale-up path.

## Adding a new creative copy of an existing server

Say you want a creative copy of `vanilla`:

1. **Add the HelmRelease** in `vanilla/helmrelease-creative.yaml` (or copy from the existing `helmrelease-creative.yaml` if adding for a different parent). Key things that MUST match the survival HR:
   - `image:` (same tag + sha)
   - `VERSION`, `SEED`, `LEVEL`, `TYPE`
   - `MODRINTH_PROJECTS` (the cloned world is mod-locked)
   - `MAX_MEMORY` can be lower (creative is lighter)

   Things that should differ:
   - `metadata.name: minecraft-<server>-creative`
   - `MODE: creative`, `FORCE_GAMEMODE: true`, `DIFFICULTY: peaceful`, `PVP: false`
   - `MOTD`, lower `MAX_MEMORY` if you want
   - `replicas: 0`
   - `CFG_LUCKPERMS_SERVER: "<server>-creative"` (distinct LuckPerms context so creative permissions don't bleed into survival)
   - Service annotations:
     ```yaml
     mc-router.itzg.me/externalServerName: "<server>-creative.games.svc.cluster.local"
     mc-router.itzg.me/autoScaleWaitTimeout: "3m"
     ```
   - `persistence.data.existingClaim: minecraft-<server>-creative`
   - Reuses the parent's `envFrom` Secret (`minecraft-<server>-secret`) and `configMap`

2. **Add the ExternalName alias** in `<server>/route-services.yaml`:
   ```yaml
   ---
   apiVersion: v1
   kind: Service
   metadata: { name: <server>-creative }
   spec:
     type: ExternalName
     externalName: minecraft-router.games.svc.cluster.local
   ```

3. **Register with Velocity** in `velocity/config/velocity.toml`:
   ```toml
   [servers]
   <Server>Creative = "<server>-creative.games.svc.cluster.local:25565"

   [forced-hosts]
   "<server>-creative.$${CFG_GAMING_DOMAIN}" = ["<Server>Creative"]
   ```

4. **Register the resource** in `<server>/kustomization.yaml` (route-services.yaml is already wired up at the parent level).

5. **Create the PVC** with the initial world clone:
   ```bash
   task minecraft:refresh-creative SOURCE=<server>
   ```

6. **Commit and reconcile**. Try `/server <Server>Creative` in-game — Velocity will dial through mc-router, which scales the STS 0→1 and connects you through.

## Refreshing creative worlds from their source

Creative worlds are **CoW clones** of the survival PVC (Rook-Ceph RBD clones). Refresh manually:

```bash
# Pull the latest survival world into the creative PVC
task minecraft:refresh-creative SOURCE=vanilla

# Optionally snapshot the existing creative PVC first for rollback
task minecraft:snapshot-creative SOURCE=vanilla
task minecraft:refresh-creative SOURCE=vanilla

# Force refresh even if a player is connected
task minecraft:refresh-creative SOURCE=vanilla FORCE=true
```

The task RCONs into the source pod to flush saves, deletes the creative PVC, and re-clones it from the source via the CSI driver. Total wall time ≈ 5-10s (no bytes copied, just metadata).

## Adding a new survival server (no creative variant yet)

1. Copy `vanilla/` as a starting point — replace mod list, version, type, seed, secret refs, configmap files.
2. Add the ExternalName + Velocity entry per the "How a backend gets routed" section above.
3. Add the Flux Kustomization in `ks.yaml`.
4. Add the 1Password secret entries for `<SERVER>_RCON_PASSWORD`, etc.

## How shared configs stay distinct per pod

`luckperms.conf` lives in **one** configMap per server family and is mounted by both the survival and creative HRs. The line `server = "${CFG_LUCKPERMS_SERVER}"` is a placeholder; itzg's `sync-and-interpolate` step copies `/config → /data/config` at pod startup, substituting any `${CFG_*}` token from the pod's env. The survival HR sets `CFG_LUCKPERMS_SERVER: "vanilla"`, the creative HR sets `CFG_LUCKPERMS_SERVER: "vanilla-creative"`, so the two pods end up with different LuckPerms server contexts even though they share the configMap.

Apply the same pattern for any other per-pod config divergence — put a `${CFG_FOO}` placeholder in the shared file, set the env var differently in each HR. Flux postBuild escape: write `$${CFG_FOO}` in source so Flux leaves `${CFG_FOO}` literal in the rendered configMap.

## Caveats

- **Mod drift between survival and creative**: their MODRINTH_PROJECTS lists are duplicated in the two HelmReleases. When updating mods, change both. The cloned world data assumes the running mods match the world it was saved from.
- **First-connect timeout**: Velocity's `connection-timeout=60000` accommodates pod cold start. If your cold start exceeds 60s (e.g. first-time mod download), Velocity gives up. After the first successful boot the image is cached on the node so subsequent cold starts are quick.
- **Lobby is single point of failure for `try`**: if Lobby is down/unresponsive, players logging into Velocity get bounced through the `try` list. Vanilla is in the list as fallback but it scales to zero — if both Lobby and Vanilla are cold, login waits.
- **mc-router doesn't pass UDP**: voicechat (UDP 24454) and Bedrock (UDP 19132) go directly to Velocity's LoadBalancer; they don't benefit from scale-from-zero. The relevant backend pods need to be up for voice to work.
