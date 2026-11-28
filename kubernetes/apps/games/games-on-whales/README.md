# games-on-whales

Moonlight game streaming from the RTX 3090, built on
[Fenrir](https://github.com/games-on-whales/fenrir), Games on Whales'
Kubernetes operator for [Wolf](https://games-on-whales.github.io/wolf/). A
paired Moonlight client sees the Apps in its person's Profile. Launching one
starts a pod for it on the GPU node, and wolf encodes that pod's display with
NVENC and streams it. Several people can stream at once, each in their own
session on the one card.

**Disabled.** `./games-on-whales/ks.yaml` stays commented out in
`../kustomization.yaml` until the NVIDIA node is in the cluster. The GPU
plumbing it depends on (`kube-system/nvidia-gpu-dra-driver`,
`kube-system/generic-device-plugin`) is enabled and inert: both select only
nodes labelled `node.kubernetes.io/gpu-vendor=nvidia`, and none is yet.

Nothing here has streamed on this cluster yet. The NVIDIA settings come from
[rothgar/homelab-flux](https://github.com/rothgar/homelab-flux/tree/main/clusters/wrtk8s/fenrir),
which streams from an RTX 3060 on Talos with the same Fenrir code. Treat the
first launch as the test.

## How it fits together

```
Moonlight ──47984/47989──▶ moonlight-proxy ── Pairing/Profile/App ──▶ operator
   │                                                                    │
   │                                              Lobby + StatefulSet <profile>-<app>
   │                                                                    ▼
   └──RTSP 48010, RTP/UDP──▶ wolf-agent pod ◀── Wayland/Pulse sockets ── session pod
                             (wolf + DRA agent)       (via CDI)          (Steam, …)
```

All on the GPU node, on the host network.

| Piece | What it does |
| --- | --- |
| `moonlight-proxy` | GameStream HTTP/HTTPS: pairing, app list, launch. Records each client as a `Pairing`. |
| `operator` | Turns a launch into a `Lobby` and a StatefulSet built from the App's pod template. |
| `wolf-agent` / `wolf` | The compositor and NVENC encoder, one per node, shared by every session. |
| `wolf-agent` / `agent` | A DRA driver, `wolf.dra.io`. It publishes lobby slots, and gives each session pod its lobby's Wayland and PulseAudio sockets through CDI. |
| `image-prepull` | Keeps the Steam image on the node so a launch beats the proxy's 25s deadline. |

Images and CRDs are pinned to [axolotlite/fenrir](https://github.com/axolotlite/fenrir)
`3f75c1aa`: upstream's unreleased DRA rework
([games-on-whales/fenrir#56](https://github.com/games-on-whales/fenrir/pull/56))
plus fixes. Renovate is told to leave the three Fenrir images alone (see
"Upgrading").

### Choices that are not obvious

- **DRA only, no NVIDIA device plugin.** The 3090 reaches pods only as a
  `gpu.nvidia.com` claim. wolf and every session pod share one claim,
  `wolf-gpu` (`resourceclaim.yaml`), so they share the card. A device plugin
  as well would advertise the same GPU twice, and the scheduler would think
  there is room when there is not.
- **Host network, not a LoadBalancer.** Moonlight connects RTSP to the
  address it paired with and takes only the port from the launch reply, so
  the proxy and wolf must share an IP. Clients add the **GPU node's IP**. It
  also keeps GameStream's UDP off a BGP hop.
- **CDI specs go to `/var/cdi/dynamic`.** containerd here reads only
  `/var/cdi/static` and `/var/cdi/dynamic` (`talos/machineconfig.yaml.j2`).
  Both the NVIDIA driver (through a post-render patch, since its chart
  hardcodes `/var/run/cdi`) and wolf-agent (`CDI_DIR`) write there.
- **Four NVIDIA fixes, all from the reference.** Leaving any of them out
  breaks the stream:
  1. `video_producer_buffer_caps` is CUDA memory (`deviceclass.yaml`).
     Otherwise there is a black screen.
  2. libglvnd's `10_nvidia.json` is mounted in, because the DRA driver does
     not inject it. Otherwise EGL falls back to Mesa and wolf panics.
  3. `GBM_BACKENDS_PATH=/usr/lib/gbm` is set. Otherwise the DMABuf
     negotiation fails.
  4. wolf runs `:stable`. The `sha-d6d41de` that the Fenrir examples pin
     panics on NVIDIA.
- **Steam libraries are on the node's NVMe** (`openebs-hostpath`), one per
  Profile. A session pod can only run on this node anyway, and a library can
  be downloaded again. No backups.
- **The 3090 has no AV1 encoder** (NVENC AV1 starts with Ada). Set Moonlight
  clients to HEVC. A client that asks for AV1 would get wolf's software
  encoder.

## Node requirements

These belong to the Talos phase, not this directory. They are listed here
because everything above assumes them.

- **Extensions:** `siderolabs/nvidia-open-gpu-kernel-modules-production` (the
  reference uses `nonfree-kmod-nvidia-production`, and either drives a 3090),
  `siderolabs/nvidia-container-toolkit-production`, plus `siderolabs/uinput`,
  `siderolabs/uhid` and `siderolabs/joydev` for controllers.
- **Kernel modules:** `nvidia`, `nvidia_uvm`, `nvidia_drm`, `nvidia_modeset`
  and `uinput` (it is not autoloaded).
- **Sysctl:** `user.max_user_namespaces` above 0 (the reference uses
  `11255`). Talos turns user namespaces off, and Steam refuses to start
  without them.
- **Label:** `node.kubernetes.io/gpu-vendor: nvidia`. This is what everything
  here selects on. It sits under the prefix NodeRestriction allows (see
  `docs/talos.md`).
- **Taint:** `nvidia.com/gpu=present:NoSchedule`. It keeps everything else off
  the gaming node. The NVIDIA chart tolerates it by default, and
  `RuntimeClass/nvidia` adds the toleration to any pod that uses it.

DaemonSets that do not tolerate that taint (node-exporter, spegel, the Ceph
CSI node plugin) will not run on the node, as on the Framework. Nothing here
mounts Ceph, so that is fine for this app.

## Enabling

One step at a time, each with a check before the next.

1. **This PR merged.** The GPU plumbing is in and does nothing yet.

   ```sh
   kubectl get deviceclass gpu.nvidia.com
   kubectl -n kube-system get ds dra-driver-nvidia-gpu-kubelet-plugin generic-device-plugin   # DESIRED 0
   ```

2. **The node joins with the requirements above.** Then:

   ```sh
   kubectl get node <node> -L node.kubernetes.io/gpu-vendor
   kubectl -n kube-system get pods -o wide | grep -E 'dra-driver-nvidia|generic-device-plugin'
   kubectl get resourceslices -o wide | grep gpu.nvidia.com                         # one 3090
   kubectl get node <node> -o jsonpath='{.status.allocatable.devic\.es/uinput}'     # 10
   ```

   Then find the 3090's render node, which `WOLF_RENDER_NODE` in `ks.yaml`
   must name. The MS-01's Iris Xe is expected to take `renderD128`:

   ```sh
   talosctl -n <node-ip> get pcidevices | grep -i nvidia        # the 3090's PCI address
   talosctl -n <node-ip> ls -l /dev/dri/by-path                  # that address -> renderD12X
   ```

   Then prove that a pod gets the card through DRA:

   ```sh
   kubectl apply -f - <<'EOF'
   apiVersion: resource.k8s.io/v1
   kind: ResourceClaim
   metadata: {name: gpu-smoke, namespace: default}
   spec:
     devices:
       requests:
         - name: gpu
           exactly: {deviceClassName: gpu.nvidia.com}
   ---
   apiVersion: v1
   kind: Pod
   metadata: {name: gpu-smoke, namespace: default}
   spec:
     runtimeClassName: nvidia
     restartPolicy: Never
     resourceClaims: [{name: gpu, resourceClaimName: gpu-smoke}]
     containers:
       - name: smi
         image: ubuntu:24.04
         command: [nvidia-smi]
         resources: {claims: [{name: gpu}]}
   EOF
   kubectl -n default logs gpu-smoke        # the 3090, its driver version
   kubectl -n default delete pod/gpu-smoke resourceclaim/gpu-smoke
   ```

3. **Uncomment `./games-on-whales/ks.yaml`** in `../kustomization.yaml`,
   correcting `WOLF_RENDER_NODE` first if step 2 said otherwise.

   ```sh
   flux -n games get ks | grep games-on-whales                                  # both Ready
   kubectl -n games get deploy -l app.kubernetes.io/name=games-on-whales      # 3/3 ready
   kubectl get resourceslices -o wide | grep wolf.dra.io                       # MAX_LOBBIES slots
   kubectl -n games logs deploy/games-on-whales-wolf-agent -c wolf | grep -E 'nvh26|nvh264|x26'
   ```

   The last check should show the NVENC encoders (`nvh265enc`, `nvh264enc`).
   If only `x264`/`x265` appear, wolf cannot see CUDA, and every stream would
   be software-encoded.

4. **Pair and stream.** See below. Launch Test Ball first, then Steam.

## Pairing a device, adding a person

1. In Moonlight, add the GPU node's IP and pair. The proxy logs a pairing URL:
   `kubectl -n games logs deploy/games-on-whales-moonlight-proxy -f`. Open it
   and enter the PIN. Treat the URL as a credential.
2. `kubectl -n games get pairings` shows the new Pairing. Add its name under
   `spec.pairings` in that person's Profile (`app/profile.yaml`) and commit.
   Flux owns the Profile, so a `kubectl edit` does not stick.
3. A new person gets a new Profile: copy `greg`, rename it, and list their
   Apps. Their Steam library is created on their first launch. Each library
   is sized at 400Gi on a ~1TB disk (not enforced), so plan on two or three
   people.

## Known issues

From upstream and the reference, not yet seen here:

- **One live session per Profile.** Resuming a Profile's stream from a second
  device crashes wolf, which then crash-loops. Quit on one device before
  starting on another. To recover, restart wolf-agent and delete the Lobby.
- **Moonlight "Resume" fails, and repeated launch/cancel can return HTTP 500**
  for a while: the client-side API rate limit runs dry. The fixes are open
  upstream as [#67](https://github.com/games-on-whales/fenrir/pull/67) and
  [#66](https://github.com/games-on-whales/fenrir/pull/66) and are not in
  these images. Quit and relaunch rather than resume.
- **Validation is thin.** A bad App template fails silently. The reason is in
  `kubectl -n games logs deploy/games-on-whales-operator`.
- **wolf sometimes crashes.** Restart the wolf-agent pod.
- **Certificate rotation re-pairs everyone.** The certificate lasts ten
  years. If it is ever renewed, restart wolf-agent and re-pair the clients.
- **DLSS under Proton is not set up.** The Talos NVIDIA extension leaves out
  the `nvngx.dll` bridge. The reference links a workaround.

## Sharing the card with other workloads

Today the card is shared only inside this namespace: everything that names
`wolf-gpu` time-slices on it, with no limit on memory. A ResourceClaim is
namespaced, so an LLM in `llm` cannot join it.

The way to share across namespaces is the driver's consumable capacity
(`featureGates.ConsumableShares: true` and `consumableShares: memory` on the
NVIDIA chart; alpha in the driver, and the Kubernetes side is on by default
from 1.36). Each claim then reserves
an amount of the 24GB, and the scheduler packs claims onto the one GPU while
they fit. It is accounting, not enforcement. Turning it on also means
`wolf-gpu` must request an explicit `capacity.requests.memory`. A claim
without one takes all of the memory and leaves no room for anything else.

"AI only when nobody is gaming" needs something to evict the model when a
session starts, as `docker/workstation/llama-desk/gpu-guard` does on the
workstation today. Upstream's draft
[#68](https://github.com/games-on-whales/fenrir/pull/68) adds this: it taints
the node `NoExecute` while a session is live. That is a follow-up, as is
moving `qwen3.6-35b-a3b` off the workstation, which loses its GPU when the
card moves.

## Upgrading

The three Fenrir images (`ghcr.io/axolotlite/fenrir/*`, a moving `:main`
tag) and the CRD commit in `ks.yaml` change together, by hand. Renovate is
disabled for those images. Read the image's
`org.opencontainers.image.revision` label, pin the CRDs to that commit, and
take all three digests from the same build. wolf, Steam and the pre-pull
images are Renovate's as usual. When Steam's digest moves, the pre-pull
DaemonSet and the App must move together, or a launch pulls cold.

## Turning it off

Comment `./games-on-whales/ks.yaml` back out. Flux prunes the release, but
keeps the CRDs (`prune: false`) and the Profiles (prune disabled), and so the
Steam libraries the Profiles own. Before deleting any of those by hand,
cancel the sessions and look at the owner references.
