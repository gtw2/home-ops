# AGENTS.md

GitOps repository for a Talos Linux Kubernetes cluster reconciled by Flux
from `main`: a merge rolls out within minutes.

## Layout

- `kubernetes/apps/<namespace>/<app>/`: `ks.yaml` holds the app's Flux
  Kustomizations (`dependsOn`, `path`, `postBuild`); `app/` holds the
  HelmRelease, its OCIRepository and the rest of its manifests.
- `kubernetes/components/`: shared Kustomize components (`common`,
  `kopiur` backups, `zeroscaler`).
- `kubernetes/flux/`: the root Flux Kustomization and shared sources.
- `talos/`: Talos machine config, rendered from `machineconfig.yaml.j2`
  by minijinja (`.taskfiles/talos`).
- `docker/sentinel/`: Compose stacks for the external VPS, not the cluster.
- `.taskfiles/`, `Taskfile.yaml`: operational tasks. Tools are managed by
  mise (`.mise.toml`).

## Conventions

- Secrets come from 1Password through ExternalSecrets. Files matching
  `*.sops.yaml` are SOPS-encrypted: never decrypt, print or edit them.
- `${SECRET_*}` values are substituted by Flux from `cluster-secrets` at
  apply time.
- Images are pinned by tag and digest. Renovate's config is
  `.renovaterc.json5` and `.renovate/`.
- Comments in manifests record why a value was chosen, often from
  measurements (memory, slots, timeouts). Keep them accurate when changing
  the value they describe.
- Commits and PR titles follow Conventional Commits
  (`fix(container): ...`, `chore(renovate): ...`).
