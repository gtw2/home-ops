# Garage

Single-node Garage cluster replacing MinIO. See `docs/garage-migration.md` (or wherever this doc has moved to post-cutover) for the full plan.

## Storage layout

| Path | Backing | Notes |
|---|---|---|
| `/mnt/meta` | `ceph-block` PVC (`garage-meta`, 5Gi RWO) | LMDB index, node key, layout, buckets and keys. POSIX fsync required — **never put this on NFS.** Backed up hourly by kopiur (see [Backups](#backups)). |
| `/mnt/data` | NFS `tank.internal:/mnt/silo/k8s/garage` | Blob chunks. Moved off `nas.internal:/mnt/sega/k8s-garage`; the export is owned 1000:1000, and NFS mounts get no kubelet fsGroup walk, so ownership must be correct on the NAS side. |

## Bootstrap (one-time, imperative)

Garage does not auto-assign storage to a node — even a single-node cluster boots with `NO ROLE ASSIGNED` and refuses writes until a layout is applied.

```sh
POD=$(kubectl -n storage get pod -l app.kubernetes.io/name=garage -o jsonpath='{.items[0].metadata.name}')

# 1. Find the node ID (long hex string)
kubectl -n storage exec -it "$POD" -- /garage status

# 2. Stage a layout — substitute the first ~8 chars of the node ID
kubectl -n storage exec -it "$POD" -- /garage layout assign \
  -z dc1 -c 200G <NODE_ID_PREFIX>

# 3. Review and apply
kubectl -n storage exec -it "$POD" -- /garage layout show
kubectl -n storage exec -it "$POD" -- /garage layout apply --version 1

# 4. Confirm the node now has a role
kubectl -n storage exec -it "$POD" -- /garage status
```

This is only for a brand-new Garage with no backup. On a rebuild the meta PVC is restored by kopiur with the node key and layout intact, so skip these steps (see [Backups](#backups)).

## Backups

The blocks on NFS are useless on their own: they are compressed chunks stored by
content hash, and only the metadata index knows which chunks make up which
object in which bucket. Losing `garage-meta` with no backup loses every object
in Garage — CNPG, immich and kritika database backups, and oCIS user files —
even though the chunks are still on the NAS.

`garage-meta` is backed up by the kopiur component (`APP: garage-meta` in
`ks.yaml`), hourly, to the `nas` repository as `garage-meta@storage:/data`. On a
fresh cluster the PVC is created from the `Restore` populator, so Garage starts
with its node key, layout, buckets, keys and index from the last snapshot.

Each backup is taken from a crash-consistent CSI snapshot, which is why
`metadata_fsync = true` is set: with fsync off, LMDB can be corrupt after an
unclean shutdown. If the restored `db.lmdb` still fails to open, fall back to
the newest file in `snapshots/` (written every 6h by
`metadata_auto_snapshot_interval`); the snapshot recovery steps are in Garage's
[recovery docs](https://garagehq.deuxfleurs.fr/documentation/operations/recovering/).

Anything written after the restored point is missing from the index: newer WAL
segments and oCIS files.

## Buckets and keys

Once the node has a role, create the buckets and per-consumer keys:

```sh
kubectl -n storage exec -it "$POD" -- /garage bucket create postgresql
kubectl -n storage exec -it "$POD" -- /garage bucket create ocis-data

kubectl -n storage exec -it "$POD" -- /garage key create cnpg-key
kubectl -n storage exec -it "$POD" -- /garage key create ocis-key
# capture the access-key + secret-key for each — paste into 1P items
#   garage-cnpg -> AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (from cnpg-key)
#   garage-ocis -> AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (from ocis-key)

kubectl -n storage exec -it "$POD" -- /garage bucket allow \
  --read --write --owner postgresql --key cnpg-key
kubectl -n storage exec -it "$POD" -- /garage bucket allow \
  --read --write --owner ocis-data --key ocis-key
```

## WebUI

`https://garage.${SECRET_DOMAIN}` — internal-only. Reads the admin API at port 3903 using the `GARAGE_ADMIN_TOKEN` mounted from `garage-secret`.

## Secrets

`garage-secret` is rendered by the `garage` ExternalSecret from 1Password item `garage`. Keys:

- `GARAGE_RPC_SECRET` ← `RPC_SECRET` (32-byte hex)
- `GARAGE_ADMIN_TOKEN` ← `ADMIN_TOKEN`
- `GARAGE_METRICS_TOKEN` ← `METRICS_TOKEN`

Per-consumer S3 access keys (`garage-cnpg`, `garage-ocis`) are populated in 1Password manually after running `garage key create` above; the consuming apps' own ExternalSecrets pull them.
