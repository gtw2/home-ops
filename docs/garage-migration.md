# MinIO → Garage migration

Plan of record for replacing MinIO with [Garage](https://garagehq.deuxfleurs.fr/), preserving data for every consumer, and modernising the CloudNativePG backup stack on the way through.

Steps marked **`YOU →`** require human action (1Password, `kubectl exec`, `mc`, etc.). Everything else is a Flux-managed change applied by committing files in this repo.

---

## 1. Current state

| Consumer | Endpoint | Bucket | Notes |
|---|---|---|---|
| `cnpg/postgres17` (Barman) | `minio.storage.svc.cluster.local:9000` | `postgresql/` (serverName `postgres17-v3`) | In-tree `spec.backup.barmanObjectStore`. Uses MinIO root creds. |
| `cnpg/immich17` (Barman) | same | `postgresql/` (serverName `immich17-v5`) | Shares bucket. Same root creds. No `ScheduledBackup` resource. |
| `ocis` (s3ng) | same | `ocis-data` | All file blobs. Metadata in `ocis` PVC (Rook-Ceph). |
| `volsync/kopia` | n/a (filesystem on NFS) | n/a | **Not** on MinIO — no migration needed. |

MinIO itself: app-template HelmRelease in `storage`, NFS-backed (`nas.internal:/mnt/sega/k8s` via `minio-nfs` PV/PVC). Routes: `minio.${SECRET_DOMAIN}` (console), `s3.${SECRET_DOMAIN}` (API). 1Password item `minio-secret` provides the root credentials, which are reused (anti-pattern) by both CNPG `cloudnative-pg-secret` and the `immich-secret`.

The `volsync` ExternalSecret has commented-out `MINIO_*` references — dead, but worth deleting at the end.

## 2. Target state

- **Garage** single-node, `replication_factor = 1`, replacing MinIO. Storage on `ceph-block` (metadata + data — Garage needs POSIX-compliant fsync, NFS is a footgun for `meta_dir`).
- **Garage WebUI** (`khairul169/garage-webui`) on `garage.${SECRET_DOMAIN}` (internal only).
- **`s3.${SECRET_DOMAIN}`** repointed to Garage at the end of the cutover window.
- **CNPG plugin-barman-cloud** operator deployed; clusters migrated off the deprecated in-tree `barmanObjectStore` API onto `ObjectStore` CRDs + `cluster.spec.plugins`.
- **Per-app S3 credentials**: separate Garage keys for CNPG and oCIS, stored as new 1Password items (`garage`, `garage-cnpg`, `garage-ocis`). The shared root-cred anti-pattern goes away.
- **`ScheduledBackup`** resources for both `postgres17` and `immich17` (currently only postgres17 has one).

---

## 3. Pre-flight verification (do these before writing any code)

These are the assumptions the rest of the plan rests on. If any of them fail, stop and revisit before proceeding.

- **`YOU →`** Confirm CNPG operator version. Chart `0.28.0` from `ghcr.io/szinn/charts-mirror/cloudnative-pg` should map to operator ≥ 1.26 (required for the barman-cloud plugin):
  ```sh
  kubectl -n database get deploy cloudnative-pg -o jsonpath='{.spec.template.spec.containers[0].image}'
  ```
  If < 1.26, bump the chart version first in a separate PR.

- **`YOU →`** Confirm a chart for the barman-cloud plugin is reachable. Check both the szinn mirror and upstream:
  ```sh
  helm show chart oci://ghcr.io/szinn/charts-mirror/plugin-barman-cloud 2>/dev/null
  helm show chart oci://ghcr.io/cloudnative-pg/charts/plugin-barman-cloud
  ```
  Pick whichever exists. Note the latest tag.

- **`YOU →`** Confirm the plugin name and CRD `apiVersion` published by the version you'll install. Run after install:
  ```sh
  kubectl get crd | grep barmancloud
  kubectl get pods -n cnpg-system | grep barman
  ```
  This document assumes `barmancloud.cnpg.io/v1` and plugin name `barman-cloud.cloudnative-pg.io`. Verify against the chart README before committing the cluster spec rewrite.

- **`YOU →`** Confirm Garage port assignments for the version you pin. Defaults at the time of writing:
  - `3900` — S3 API
  - `3901` — RPC (cluster internal — never expose)
  - `3902` — S3 web (static-site bucket serving — not used here)
  - `3903` — admin API (used by the WebUI)

  Check `https://garagehq.deuxfleurs.fr/documentation/reference-manual/configuration/` for the version you select.

- **`YOU →`** Inventory current data sizes:
  ```sh
  mc du minio/postgresql
  mc du minio/ocis-data
  mc ls -r --json minio/ocis-data | jq -s 'length'   # object count
  ```
  Make sure the Ceph pool you target for Garage has enough free capacity (`kubectl rook-ceph ceph df` or via the Rook dashboard).

- **`YOU →`** Confirm there's no in-flight oCIS upload activity:
  ```sh
  kubectl -n storage logs deploy/ocis --tail=200 | grep -i upload
  ```
  Schedule the oCIS cutover (Phase 4b) for a quiet window — `OCIS_ASYNC_UPLOADS=true` means partially-uploaded objects exist as PVC metadata + missing blobs, and the s3 mirror will skip what isn't there yet.

---

## 4. Phase 0 — Pre-flight (no cluster changes)

1. **`YOU →`** Take a manual base backup on each cluster while still on MinIO so a known-good restore point exists on the bucket you're about to mirror:
   ```sh
   kubectl cnpg backup postgres17 -n database
   kubectl cnpg backup immich17  -n media
   kubectl cnpg status postgres17 -n database | head -40
   kubectl cnpg status immich17  -n media     | head -40
   ```
   Wait for both `Last Successful Backup` timestamps to be recent.

2. **`YOU →`** Snapshot the oCIS PVC (Rook-Ceph supports VolumeSnapshots). The oCIS metadata in the PVC must stay aligned with the s3 blobs; a snapshot is your rollback if Phase 4b goes wrong:
   ```sh
   kubectl -n storage get pvc ocis -o yaml | grep -A2 storageClassName
   # then create a VolumeSnapshot referencing the matching VolumeSnapshotClass
   ```
   (If snapshots are disabled per `openebs/app/helmrelease.yaml`, skip — but plan to do `mc mirror` twice during cutover instead.)

3. **`YOU →`** Create three new 1Password items (do **not** populate the access keys yet — those are generated by Garage in Phase 2):

   | 1P Item | Fields | Used by |
   |---|---|---|
   | `garage` | `RPC_SECRET` (32-byte hex), `ADMIN_TOKEN` (random), `METRICS_TOKEN` (random) | Garage server + WebUI |
   | `garage-cnpg` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | postgres17 + immich17 backup |
   | `garage-ocis` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | oCIS s3ng |

   Generate the Garage tokens locally:
   ```sh
   openssl rand -hex 32   # RPC_SECRET
   openssl rand -hex 24   # ADMIN_TOKEN
   openssl rand -hex 24   # METRICS_TOKEN
   ```

---

## 5. Phase 1 — Stand up Garage and Garage WebUI alongside MinIO

Create `kubernetes/apps/storage/garage/` with this layout:

```
kubernetes/apps/storage/garage/
├── ks.yaml
├── readme.md
└── app/
    ├── kustomization.yaml
    ├── externalsecret.yaml      # pulls 1P 'garage' into garage-secret
    ├── pvc.yaml                 # meta + data PVCs (ceph-block)
    ├── configmap.yaml           # garage.toml templated from secret
    ├── helmrelease-garage.yaml  # app-template, single replica
    ├── helmrelease-webui.yaml   # khairul169/garage-webui
    └── httproute.yaml           # garage.${SECRET_DOMAIN} -> webui (internal)
```

Key choices baked in:

- **Image**: `dxflrs/garage` pinned by digest. Renovate will manage updates.
- **`garage.toml`**: rendered from a ConfigMap (or templated Secret) that injects `rpc_secret`, `admin_token`, `metrics_token` via env-substitution. Set `replication_factor = 1`, `consistency_mode = "none"`, `db_engine = "lmdb"`, `metadata_dir = "/mnt/meta"`, `data_dir = "/mnt/data"`.
- **PVCs** (both `ceph-block`, RWO):
  - `garage-meta`: 5Gi
  - `garage-data`: size for current MinIO usage + 50% headroom (check Phase 3 inventory)
- **Service**: ClusterIP, ports 3900 (s3), 3903 (admin). **Do not expose 3901**.
- **Routes**:
  - `garage-s3.${SECRET_DOMAIN}` → port 3900 on `envoy-internal` (temporary; required only if you want to mirror via external endpoints — you can skip this and do all mirroring pod-to-pod instead).
  - `garage.${SECRET_DOMAIN}` → WebUI (port from the WebUI Helm chart's service).
- **WebUI**: `ghcr.io/khairul169/garage-webui`, env:
  - `API_BASE_URL=http://garage.storage.svc.cluster.local:3903`
  - `S3_ENDPOINT_URL=http://garage.storage.svc.cluster.local:3900`
  - admin token from the `garage-secret`. **Internal-only HTTPRoute.** Front with Authelia later if you want per-user auth; otherwise rely on internal-only access.

After Flux applies:

1. **`YOU →`** Verify the pod is running:
   ```sh
   kubectl -n storage get pods -l app.kubernetes.io/name=garage
   kubectl -n storage logs deploy/garage --tail=100
   ```
2. **`YOU →`** Bootstrap the Garage cluster layout (one-time, imperative — Garage will not assign storage to the node automatically):
   ```sh
   POD=$(kubectl -n storage get pod -l app.kubernetes.io/name=garage -o jsonpath='{.items[0].metadata.name}')

   kubectl -n storage exec -it "$POD" -- /garage status
   # Note the node ID printed under "Healthy nodes". You'll need its short prefix.

   kubectl -n storage exec -it "$POD" -- /garage layout assign \
     -z dc1 -c 200G <NODE_ID_PREFIX>
   kubectl -n storage exec -it "$POD" -- /garage layout show
   kubectl -n storage exec -it "$POD" -- /garage layout apply --version 1
   kubectl -n storage exec -it "$POD" -- /garage status
   ```
   Substitute `200G` with the data PVC size you provisioned.

3. **`YOU →`** Browse to `https://garage.${SECRET_DOMAIN}` and confirm the WebUI loads and reports the node as healthy.

> **Risk**: if the Garage pod restarts before `garage layout apply` succeeds, the cluster boots empty. Re-run the layout commands. Document this in `kubernetes/apps/storage/garage/readme.md` as the standard recovery procedure.

---

## 6. Phase 2 — Buckets, keys, policies

Inside the Garage pod:

```sh
POD=$(kubectl -n storage get pod -l app.kubernetes.io/name=garage -o jsonpath='{.items[0].metadata.name}')
kubectl -n storage exec -it "$POD" -- sh
```

Then run **`YOU →`**:

```sh
# Buckets
/garage bucket create postgresql
/garage bucket create ocis-data

# Per-consumer keys
/garage key create cnpg-key
/garage key create ocis-key

# Capture the access-key + secret-key emitted by each `key create` command.

# Grants
/garage bucket allow --read --write --owner postgresql --key cnpg-key
/garage bucket allow --read --write --owner ocis-data  --key ocis-key

# Sanity check
/garage bucket info postgresql
/garage bucket info ocis-data
```

**`YOU →`** Paste the two key pairs into the 1Password items created in Phase 0:
- `garage-cnpg` → `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` from `cnpg-key`
- `garage-ocis` → `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` from `ocis-key`

External-Secrets will pick these up on its next sync; no Flux action needed yet.

---

## 7. Phase 3 — Mirror existing data, MinIO → Garage

Run from a debug pod in-cluster so traffic stays on the cluster network. No external routes required.

**`YOU →`**:

```sh
kubectl -n storage run mc --rm -it \
  --image=quay.io/minio/mc:latest --restart=Never -- sh

# inside the mc pod:
mc alias set src http://minio.storage.svc.cluster.local:9000  <MINIO_ROOT_USER> <MINIO_ROOT_PASSWORD>
mc alias set dst http://garage.storage.svc.cluster.local:3900 <CNPG_KEY_ID>     <CNPG_KEY_SECRET>

mc mirror --preserve-attrs --overwrite src/postgresql dst/postgresql
mc du src/postgresql
mc du dst/postgresql

# re-alias dst with the ocis key to mirror the second bucket
mc alias set dst http://garage.storage.svc.cluster.local:3900 <OCIS_KEY_ID> <OCIS_KEY_SECRET>
mc mirror --preserve-attrs --overwrite src/ocis-data dst/ocis-data
mc du src/ocis-data
mc du dst/ocis-data
```

`du` outputs should match (within rounding). If they don't, re-run `mc mirror` — it's idempotent.

> **Risk**: `mc mirror` doesn't preserve some MinIO-specific metadata that Barman doesn't care about but that S3 clients can. CNPG and oCIS only use plain object semantics, so this is fine.

---

## 8. Phase 4 — Cutover

Each consumer is cut over independently. MinIO stays running for the entire phase. Rollback for any sub-step = revert the commit + (for oCIS) restart the deployment.

### 8a. Deploy the CNPG barman-cloud plugin operator

Create `kubernetes/apps/database/cloudnative-pg-barman-plugin/`:

```
ks.yaml
app/
├── kustomization.yaml
├── ocirepository.yaml
└── helmrelease.yaml
```

The HelmRelease pulls whichever chart you confirmed in §3, deployed into `cnpg-system` (or wherever the CNPG operator currently runs — match it). Add a `dependsOn` on `cloudnative-pg`. Add the new `ks.yaml` to `kubernetes/apps/database/kustomization.yaml`.

After Flux reconciles:

**`YOU →`**:
```sh
kubectl get pods -n cnpg-system | grep barman
kubectl get crd barmancloud.cnpg.io 2>/dev/null \
  || kubectl get crd | grep barman    # confirm the actual CRD api group printed by your chart version
```

If the CRD apiVersion isn't `barmancloud.cnpg.io/v1`, adjust §8b YAML accordingly.

### 8b. Migrate `postgres17` (database namespace)

1. **Edit `kubernetes/apps/database/cloudnative-pg/app/externalsecret.yaml`**:
   - Replace `extract: key: minio-secret` with `extract: key: garage-cnpg`.
   - Replace the `aws-access-key-id`/`aws-secret-access-key` template references with the `AWS_*` field names from the new 1P item (the commented lines at `externalsecret.yaml:24-25` are already in the right shape — uncomment them and delete the `MINIO_*` lines above).

2. **Add `kubernetes/apps/database/cloudnative-pg/cluster/objectstore.yaml`**:
   ```yaml
   ---
   apiVersion: barmancloud.cnpg.io/v1
   kind: ObjectStore
   metadata:
     name: postgres-garage
     namespace: database
   spec:
     configuration:
       destinationPath: s3://postgresql/
       endpointURL: http://garage.storage.svc.cluster.local:3900
       s3Credentials:
         accessKeyId:     { name: cloudnative-pg-secret, key: aws-access-key-id }
         secretAccessKey: { name: cloudnative-pg-secret, key: aws-secret-access-key }
       data: { compression: bzip2 }
       wal:  { compression: bzip2, maxParallel: 8 }
     retentionPolicy: 30d
   ```
   Add it to `kubernetes/apps/database/cloudnative-pg/cluster/kustomization.yaml`.

3. **Rewrite `kubernetes/apps/database/cloudnative-pg/cluster/cluster17.yaml`**:
   - Delete the entire `spec.backup` block (and the `&barmanObjectStore` anchor).
   - Bump `serverName` from `postgres17-v3` → `postgres17-v4`.
   - Update `bootstrap.recovery.source` from `postgres17-v2` → `postgres17-v3` (purely documentary; ignored on a running cluster, but keeps history correct in case of a future rebuild).
   - Replace the `externalClusters` block with the plugin-style equivalent.
   - Add `spec.plugins`.

   Final shape:
   ```yaml
   spec:
     instances: 3
     # ... unchanged ...
     plugins:
       - name: barman-cloud.cloudnative-pg.io
         isWALArchiver: true
         parameters:
           barmanObjectName: postgres-garage
           serverName: postgres17-v4
     bootstrap:
       recovery:
         source: postgres17-v3
     externalClusters:
       - name: postgres17-v3
         plugin:
           name: barman-cloud.cloudnative-pg.io
           parameters:
             barmanObjectName: postgres-garage
             serverName: postgres17-v3
   ```

4. **Commit + push.** Flux applies; CNPG rolls the pods to inject the plugin sidecar.

5. **`YOU →`** Force the first backup on Garage and watch:
   ```sh
   kubectl cnpg backup postgres17 -n database --backup-name first-on-garage
   kubectl -n database get backup -w
   kubectl cnpg status postgres17 -n database
   ```
   Validate the prefix appeared on Garage:
   ```sh
   kubectl -n storage exec -it "$POD" -- /garage bucket info postgresql
   # should now show postgres17-v4/ alongside the mirrored postgres17-v3/
   ```

### 8c. Migrate `immich17` (media namespace)

ObjectStores can't be referenced cross-namespace. Repeat 8b's pattern in `media`:

1. **Edit `kubernetes/apps/media/immich/app/externalsecret.yaml`**: same swap (`minio-secret` → `garage-cnpg`, `MINIO_*` → `AWS_*` field names, lines `42-45` already pre-shaped).

2. **Add `kubernetes/apps/media/immich/db/objectstore.yaml`**: same as the `postgres-garage` ObjectStore but `metadata.name: immich-garage`, `metadata.namespace: media`, secret refs pointing at `immich-secret`. Register in `kubernetes/apps/media/immich/db/kustomization.yaml`.

3. **Rewrite `kubernetes/apps/media/immich/db/cluster.yaml`**: same surgery — delete `spec.backup`, add `spec.plugins` with `barmanObjectName: immich-garage` and `serverName: immich17-v6`, update `bootstrap.recovery.source: immich17-v5`, restructure `externalClusters` to plugin form referencing `serverName: immich17-v5`.

4. **Add `kubernetes/apps/media/immich/db/scheduledbackup.yaml`** — there isn't one today:
   ```yaml
   ---
   apiVersion: postgresql.cnpg.io/v1
   kind: ScheduledBackup
   metadata:
     name: immich
     namespace: media
   spec:
     schedule: "@daily"
     immediate: true
     backupOwnerReference: self
     cluster:
       name: immich17
   ```

5. **`YOU →`** Force a backup, verify prefix on Garage, same as 8b step 5.

### 8d. Migrate `oCIS` (storage namespace)

oCIS s3ng holds blobs in s3 *and* the matching metadata in the PVC. Both have to switch atomically.

1. **`YOU →`** Stop oCIS:
   ```sh
   kubectl -n storage scale deploy/ocis --replicas=0
   kubectl -n storage rollout status deploy/ocis --timeout=2m
   ```

2. **`YOU →`** Final mirror pass to catch any deltas since Phase 3:
   ```sh
   kubectl -n storage run mc --rm -it --image=quay.io/minio/mc:latest --restart=Never -- sh -c '
     mc alias set src http://minio.storage.svc.cluster.local:9000  '"$MINIO_USER"' '"$MINIO_PASS"'
     mc alias set dst http://garage.storage.svc.cluster.local:3900 '"$OCIS_KEY_ID"' '"$OCIS_KEY_SECRET"'
     mc mirror --preserve-attrs --overwrite src/ocis-data dst/ocis-data
   '
   ```
   (Substitute the env vars by hand or pull from 1Password — don't paste secrets into your shell history.)

3. **Add a new ExternalSecret** (or extend the existing oCIS secret) so oCIS gets `garage-ocis` keys. Then update `kubernetes/apps/storage/ocis/app/helmrelease.yaml`:
   - `STORAGE_USERS_S3NG_ENDPOINT: http://garage.storage.svc.cluster.local:3900`
   - Add `STORAGE_USERS_S3NG_ACCESS_KEY` / `STORAGE_USERS_S3NG_SECRET_KEY` from the new secret (oCIS reads these from env; current envFrom on `ocis-secret` will work if you keep the same keys there).
   - `STORAGE_USERS_S3NG_BUCKET: ocis-data` (unchanged).

4. **Commit + push.** Flux applies; oCIS comes back up automatically (deployment wasn't deleted, only scaled).

5. **`YOU →`** Validate:
   ```sh
   kubectl -n storage rollout status deploy/ocis
   kubectl -n storage logs deploy/ocis --tail=200 | grep -iE 's3|storage.users'
   ```
   Then in the UI: log in, list files (existing files should appear with content intact), upload a new file, log out and back in, confirm the new file persists. Compare object count before/after — Garage `ocis-data` count should = MinIO count + 1.

> **Risk**: if any oCIS user uploads while replicas=0, that upload is lost. Communicate the maintenance window. The `OCIS_ASYNC_UPLOADS=true` setting also means stuck partial uploads in the PVC's `uploads/` dir may exist — these are safe to leave; they retry on next start.

---

## 9. Phase 5 — Decommission MinIO

Soak period: **at least 7 days** with both stacks alive. Goals during soak:

- Multiple successful nightly `ScheduledBackup` runs land in `garage/postgresql/postgres17-v4/` and `garage/postgresql/immich17-v6/`.
- A real or simulated oCIS user-day passes without errors.

Then:

1. **`YOU →`** **Recovery dry run** — the only proof your backups are usable. Stand up a throwaway cluster from Garage:
   ```yaml
   # /tmp/recovery-test.yaml
   apiVersion: postgresql.cnpg.io/v1
   kind: Cluster
   metadata:
     name: postgres17-recovery-test
     namespace: database
   spec:
     instances: 1
     # ... mirror the prod cluster spec, point bootstrap.recovery.source at postgres17-v4 ...
   ```
   ```sh
   kubectl apply -f /tmp/recovery-test.yaml
   kubectl cnpg status postgres17-recovery-test -n database
   # confirm it bootstraps, then:
   kubectl delete cluster postgres17-recovery-test -n database
   ```

2. **Repoint `s3.${SECRET_DOMAIN}`**:
   - In `kubernetes/apps/storage/garage/app/httproute.yaml`, add `s3.${SECRET_DOMAIN}` as a hostname on the s3 route (or add a second route).
   - In `kubernetes/apps/storage/minio/app/helmrelease.yaml`, remove the `s3` route block.
   - Commit. External clients stop seeing MinIO and start seeing Garage on the same host.

3. **Delete MinIO** — once you're satisfied that nothing has hit `s3.${SECRET_DOMAIN}` and got an error:
   - Remove `kubernetes/apps/storage/minio/` from the repo.
   - Remove `./minio/ks.yaml` from `kubernetes/apps/storage/kustomization.yaml`.
   - Commit. Flux prunes the HelmRelease, Service, HTTPRoutes.

4. **`YOU →`** **Manually delete MinIO data on NFS** — Flux does not touch NFS contents:
   ```sh
   ssh nas.internal 'ls /mnt/sega/k8s/'      # confirm what's there
   ssh nas.internal 'rm -rf /mnt/sega/k8s/.minio.sys /mnt/sega/k8s/postgresql /mnt/sega/k8s/ocis-data'
   ```
   Be specific — don't `rm -rf /mnt/sega/k8s/` if other things share that path. The `minio-nfs` PV pointed there but it's possible other workloads also use the directory.

5. **`YOU →`** **Retire 1Password items**: archive `minio-secret`. Don't delete it for at least another month — keeping it as a safety reference is cheap.

6. **Clean up dead references**:
   - `kubernetes/components/volsync/externalsecret.yaml` lines 19-24 — delete the commented-out MinIO block.
   - Remove the `MINIO_BROWSER_REDIRECT_URL` / `MINIO_SERVER_URL` references in `kubernetes/apps/storage/minio/app/helmrelease.yaml` (deleted with the rest in step 3, but worth grep'ing to be sure: `grep -ri MINIO kubernetes/`).

7. **Move `docs/garage-migration.md` → `kubernetes/apps/storage/garage/readme.md`** so the runbook lives with the app.

---

## 10. CloudNativePG hardening (folded into the migration)

Already covered above; summarised here for review:

- **Per-cluster keys**: ✓ each cluster's secret pulls from `garage-cnpg` (still shared across the two CNPG clusters since they share the bucket — split further if you want strict isolation).
- **Plugin migration**: ✓ moved to `barman-cloud.cloudnative-pg.io` ahead of in-tree API removal.
- **`ScheduledBackup` for `immich17`**: ✓ added in §8c step 4.
- **Retention `30d`**: unchanged. **`YOU →`** verify Ceph pool capacity headroom for ~30d × WAL × 2 clusters × `bzip2`. If tight, drop to `14d` in both ObjectStores.
- **Recovery rehearsal documented**: ✓ in §9 step 1. Capture the actual command transcript into `kubernetes/apps/database/cloudnative-pg/readme.md` after running it the first time.
- **Backup-freshness alert** (optional, recommended): add a PrometheusRule that alerts when CNPG's `cnpg_collector_last_available_backup_timestamp` is older than 26h. There's already a `prometheusrule.yaml` in `cluster/` — extend it.

---

## 11. Risks summary

| Risk | Mitigation |
|---|---|
| oCIS s3ng + Barman both call them "buckets" but have different consistency needs | Phase 4d's scale-to-zero is non-negotiable for oCIS. Barman tolerates online copy. |
| `MINIO_ROOT_USER` is currently the same key as `aws-access-key-id` for two consumers | Don't rotate `minio-secret` mid-migration; both apps would lose access at once. |
| Garage layout assignment is imperative | Phase 1 step 2 is required after every fresh deploy of Garage. Document in app readme. |
| NFS for Garage `meta_dir` is a known footgun | Plan uses `ceph-block` for both meta and data. Don't switch to NFS without testing. |
| Single-node Garage `replication_factor = 1` = no Garage-layer redundancy | Ceph already provides redundancy for the underlying PVCs. Volsync/kopia keeps off-cluster backups of `data_dir`. |
| In-tree `barmanObjectStore` and plugin can't coexist on one cluster | Plan deletes `spec.backup` in the same commit that adds `spec.plugins`. No middle state. |
| `bootstrap.recovery` is consulted only at cluster creation | Updating it is documentary — actually-running clusters ignore it. Don't expect it to do anything for the migration itself. |

## 12. Rollback per phase

| Phase | Rollback |
|---|---|
| 1 (deploy Garage) | `git revert` the Garage app commit. Flux removes it. MinIO unaffected. |
| 2 (buckets/keys) | Drop the buckets via `garage bucket delete --yes`. No external impact. |
| 3 (mirror) | No-op — the mirror is a copy. Just delete the Garage objects if you want a clean slate. |
| 4b (postgres17 cutover) | `git revert` the cluster17 + ObjectStore + externalsecret commits. CNPG re-rolls back to in-tree barmanObjectStore on MinIO. Existing `postgres17-v4` prefix on Garage is harmless. |
| 4c (immich17 cutover) | Same pattern. |
| 4d (oCIS cutover) | Scale oCIS to 0, restore the pre-cutover commit, scale back to 1. If blob/metadata diverged, restore the PVC from the snapshot taken in Phase 0 step 2. |
| 5 (decommission) | Don't roll back — that's why §9 step 1 (recovery dry run) is required before this step. If you haven't deleted MinIO data on NFS yet, you can re-add the MinIO HelmRelease and the data is still there. |

---

## 13. File change inventory

**New**:
- `kubernetes/apps/storage/garage/ks.yaml`
- `kubernetes/apps/storage/garage/readme.md`
- `kubernetes/apps/storage/garage/app/{kustomization,externalsecret,pvc,configmap,helmrelease-garage,helmrelease-webui,httproute,ocirepository}.yaml`
- `kubernetes/apps/database/cloudnative-pg-barman-plugin/{ks.yaml,app/kustomization.yaml,app/ocirepository.yaml,app/helmrelease.yaml}`
- `kubernetes/apps/database/cloudnative-pg/cluster/objectstore.yaml`
- `kubernetes/apps/media/immich/db/objectstore.yaml`
- `kubernetes/apps/media/immich/db/scheduledbackup.yaml`

**Edited**:
- `kubernetes/apps/storage/kustomization.yaml` — add garage, eventually remove minio
- `kubernetes/apps/database/kustomization.yaml` — add cloudnative-pg-barman-plugin
- `kubernetes/apps/database/cloudnative-pg/app/externalsecret.yaml` — switch to `garage-cnpg`
- `kubernetes/apps/database/cloudnative-pg/cluster/cluster17.yaml` — drop `spec.backup`, add `spec.plugins`, bump serverName
- `kubernetes/apps/database/cloudnative-pg/cluster/kustomization.yaml` — register objectstore.yaml
- `kubernetes/apps/media/immich/app/externalsecret.yaml` — switch to `garage-cnpg`
- `kubernetes/apps/media/immich/db/cluster.yaml` — drop `spec.backup`, add `spec.plugins`, bump serverName
- `kubernetes/apps/media/immich/db/kustomization.yaml` — register objectstore.yaml + scheduledbackup.yaml
- `kubernetes/apps/storage/ocis/app/helmrelease.yaml` — endpoint + s3 creds env
- `kubernetes/apps/storage/ocis/app/externalsecret.yaml` (or new file) — `garage-ocis` keys
- `kubernetes/components/volsync/externalsecret.yaml` — delete dead minio comments

**Deleted (Phase 5)**:
- `kubernetes/apps/storage/minio/` (entire directory)
- 1Password item `minio-secret` (after a month's grace)

---

## 14. External actions checklist (`YOU →` items, in order)

This is the same content as above, condensed into a single checklist for execution day.

- [ ] Pre-flight: verify CNPG operator ≥ 1.26 (§3)
- [ ] Pre-flight: confirm plugin chart source + version (§3)
- [ ] Pre-flight: confirm CRD apiVersion + plugin name (§3, after install in §8a)
- [ ] Pre-flight: confirm Garage port assignments for the version pinned (§3)
- [ ] Pre-flight: `mc du` baseline + Ceph pool capacity check (§3)
- [ ] Pre-flight: confirm no in-flight oCIS uploads (§3)
- [ ] Phase 0: manual backups for both CNPG clusters (§4 step 1)
- [ ] Phase 0: snapshot oCIS PVC (§4 step 2)
- [ ] Phase 0: create three new 1Password items (§4 step 3)
- [ ] Phase 1: bootstrap Garage layout via `kubectl exec` (§5 after deploy)
- [ ] Phase 1: verify WebUI reachable at `garage.${SECRET_DOMAIN}` (§5)
- [ ] Phase 2: create buckets, keys, grants in Garage (§6)
- [ ] Phase 2: paste keys into 1Password items (§6)
- [ ] Phase 3: mirror both buckets via in-cluster `mc` debug pod (§7)
- [ ] Phase 4a: confirm plugin operator + CRD names after deploy (§8a)
- [ ] Phase 4b: force a backup on `postgres17`, verify prefix on Garage (§8b step 5)
- [ ] Phase 4c: force a backup on `immich17`, verify prefix on Garage (§8c step 5)
- [ ] Phase 4d: scale oCIS to 0, final mirror, scale back, validate UI (§8d)
- [ ] Soak ≥ 7 days
- [ ] Phase 5: recovery dry run (§9 step 1)
- [ ] Phase 5: repoint `s3.${SECRET_DOMAIN}` (§9 step 2)
- [ ] Phase 5: delete MinIO HelmRelease (§9 step 3)
- [ ] Phase 5: delete MinIO data on NFS (§9 step 4)
- [ ] Phase 5: archive `minio-secret` in 1Password (§9 step 5)
- [ ] Phase 5: clean up dead references (§9 step 6)
- [ ] Phase 5: relocate this doc to `kubernetes/apps/storage/garage/readme.md` (§9 step 7)
