# Garage

Single-node Garage cluster replacing MinIO. See `docs/garage-migration.md` (or wherever this doc has moved to post-cutover) for the full plan.

## Storage layout

| Path | Backing | Notes |
|---|---|---|
| `/mnt/meta` | `ceph-block` PVC (`garage-meta`, 5Gi RWO) | LMDB index. POSIX fsync required — **never put this on NFS.** |
| `/mnt/data` | NFS `nas.internal:/mnt/sega/k8s-garage` | Blob chunks. Same backing tier as MinIO previously used. |

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

If the meta PVC is ever wiped (or you delete and recreate the cluster), re-run these steps. The data on NFS is preserved — but Garage won't index it until the layout exists.

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
