# Sentinel

Bootstrap the OVH VPS (`40.160.90.34`, Vint Hill VA) that fronts the cluster's
Towonel agents and runs the out-of-cluster Gatus.

Ansible prepares the host and starts `doco-cd`; `doco-cd` then reconciles the
Compose stacks under `docker/sentinel/` from this repo. Ansible is for things
that cannot come from git: host packages, the firewall, and the two rendered
secret files.

## Run it

Two terminals. The first holds a port-forward open for the whole run:

```bash
task sentinel:connect      # terminal 1, leave it running
```

```bash
task sentinel:deps         # terminal 2, once
task sentinel:check        # dry run, changes nothing
task sentinel:apply
```

`sentinel:check` and `sentinel:apply` both refuse to start if the forward is
down or `OP_CONNECT_TOKEN` is unset, so a forgotten terminal fails immediately
rather than halfway through provisioning.

## Why the port-forward

The playbook's 1Password lookups do NOT use the `op` CLI - there is no account
configured on this machine and none is needed. `community.general.onepassword`
reads `OP_CONNECT_HOST` and `OP_CONNECT_TOKEN` straight from the environment and
talks to the same 1Password Connect server external-secrets already uses.

That server is `ClusterIP`-only, so it is unreachable from a workstation without
help. `task sentinel:connect` forwards it to `localhost:8080`, which is what
`OP_CONNECT_HOST` in `.secrets.env` points at. Exposing Connect permanently via
an HTTPRoute would remove the forward, at the cost of putting the secrets API on
the LAN - not worth it for something run this rarely.

`.secrets.env` is gitignored and loaded by mise (`_.file`). Regenerate it any
time with:

```bash
task sentinel:secrets && exec $SHELL
```

The shell reload matters: mise reads that file when the environment is built, so
a freshly written token is not visible to an already-open shell.

Items are in the **`homeops`** vault. `SECRET_DOMAIN` does not come from
1Password at all - it is decrypted from
`kubernetes/components/common/sops/cluster-secrets.sops.yaml`, and mise already
exports `SOPS_AGE_KEY_FILE`, so that needs no extra setup.

## What is rendered, not committed

| Path on the VPS | From | Holds |
|---|---|---|
| `/opt/sentinel/env` | sops + 1Password | `SECRET_DOMAIN`, hub public URL, edge addresses, ACME email |
| `/opt/gatus/secrets.yaml` | 1Password | Pushover credentials, Alertmanager heartbeat token |

Both are mode `0400`. Every Compose stack reads the first via `env_file`.

## After the first run

Read the hub's operator key and store it in 1Password as item `towonel`, field
`TOWONEL_API_KEY` — this is what the Kubernetes operator authenticates with:

```bash
ssh ubuntu@40.160.90.34
sudo docker exec towonel-hub cat /data/operator.key
```

It is the account-level key, not an invite token: the operator provisions
invites itself.

## Adding a tunnelled game server

Two files, both here, and forgetting either fails silently:

1. a `ports:` line in `docker/sentinel/01-towonel/docker-compose.yaml`
2. a matching rule in the `Allow public edge ports` loop in `playbook.yaml`

Then the `towonel.io/<portName>.*` annotations on the Service in `kubernetes/`.

## Back up

`/opt/towonel/data` — `operator.key`, `invite_hash.key`, `hub.db`, `node.key`.
Losing these means re-enrolling every agent. `doco-cd` keeps its generated API
and webhook secrets in `/opt/doco-cd`; those are regenerable.
