# Sentinel

Bootstrap the OVH VPS (`40.160.90.34`, Vint Hill VA) that fronts the cluster's
Towonel agents and runs the out-of-cluster Gatus.

Ansible prepares the host and starts `doco-cd`; `doco-cd` then reconciles the
Compose stacks under `docker/sentinel/` from this repo. Ansible is for things
that cannot come from git: host packages, the firewall, and the two rendered
secret files.

## Run it

```bash
ansible-galaxy collection install -r ansible/sentinel/requirements.yaml
ansible-playbook -i ansible/sentinel/inventory.yaml ansible/sentinel/playbook.yaml
```

`ansible` comes from mise. The playbook decrypts `SECRET_DOMAIN` out of
`kubernetes/components/common/sops/cluster-secrets.sops.yaml` on the control
machine, so the domain is never committed under `docker/`; mise already exports
`SOPS_AGE_KEY_FILE`, so that works inside this repo with no extra environment.
The 1Password lookups need the `op` CLI signed in.

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
