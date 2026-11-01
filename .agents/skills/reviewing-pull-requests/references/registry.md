# Reading a registry

Read a container image or an OCI chart from its registry rather than
guessing from its tag. In kritika, `fetch_oci` does this: give it
`<registry>/<repository>:<tag>` or `@<digest>`, or `tags` to list them.
Elsewhere, use `crane`, which reads registries anonymously and prints JSON.

- `crane ls <registry>/<repo>` lists tags. `sha256-…` tags are signatures
  and attestations, not versions.
- `crane digest <image>:<tag>` resolves a tag to its digest, to compare
  with the digest pinned beside the tag.
- `crane manifest <image>:<tag>` lists a multi-platform image's
  platforms. Every node is `linux/amd64`, so a new tag without it is a
  finding.
- `crane config --platform linux/amd64 <image>@<digest>` shows when the
  image was built (`created`), how it starts (`Entrypoint`, `Cmd`, `User`,
  `Env`) and its labels: `org.opencontainers.image.source` names the
  upstream project, `org.opencontainers.image.version` the version, and
  `org.opencontainers.image.revision` the commit it was built from.
- On `ghcr.io/home-operations/*` images, `source` names the application's
  repository but `revision` is a `home-operations/containers` commit: its
  message names the version it built, and its diff says why a digest-only
  update was rebuilt.
- `crane config <chart>:<version>` on an `OCIRepository` chart prints its
  `Chart.yaml` as JSON: `version`, `appVersion` and `sources`, at the old
  and the new tag.

The config says how an image starts, not what is in it. Read a file the
entrypoint runs in the upstream repository at the tag (`fetch_repo` in
kritika). Outside kritika,
`crane export --platform linux/amd64 <image>@<digest> <file>.tar` writes
the image's filesystem to a tar file to list, which is how a file missing
from a new tag shows.

Docker Hub (`docker.io`, or an image with no registry) is not among the
hosts kritika's gateway allows, so `fetch_oci` cannot read those images.
`mirror.gcr.io` serves only the Docker Hub images it has cached and
answers 429 for the rest, `mbround18/valheim` and
`sknnr/enshrouded-dedicated-server` among them.
