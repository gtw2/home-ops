# Rendering a HelmRelease with flate

flate renders this repository's HelmReleases and Kustomizations offline,
with the new chart and this repository's values. Render from the whole
tree, narrowed to the app's namespace, from the repository root:
`--path kubernetes/flux/cluster -n <namespace>`. A namespace's own
directory (`--path kubernetes/apps/<namespace>`) blocks every
Kustomization that depends, directly or through another, on one in
another namespace (more than half of them), and leaves out the defaults
`cluster-apps` patches into every HelmRelease.

Run the test first. It passes or fails in a few lines:

```
flate test hr <name> -n <namespace> --path kubernetes/flux/cluster --no-progress
```

With git history, diff the HelmRelease against the base, which flate
renders from that revision in a temporary directory. The diff shows the
images and values that move without appearing in the PR's diff:

```
flate diff hr <name> -n <namespace> --base origin/main --path kubernetes/flux/cluster --no-progress -o diff
```

Without history, as in kritika's checkout, build the head alone:

```
flate build hr <name> -n <namespace> --path kubernetes/flux/cluster --no-progress
```

A render is often past the 32 KiB a command's output is cut to (kritika's
own is 131 KiB). Drop the bulky kinds with
`--skip-kinds ConfigMap,GrafanaDashboard,PrometheusRule` (23 KiB for
kritika), or keep one template with `--show-only <chart>/templates/<file>`:
the chart's name comes first, and `templates/<file>` alone shows nothing.

`flate get images -n <namespace> --path kubernetes/flux/cluster --no-progress`
lists the images the namespace runs, from its pod specs.

flate reports every failure in the namespace, not only the requested
HelmRelease's, and exits nonzero for any of them. A failure is a finding
on the changed line only when it belongs to the HelmRelease under review
and the change caused it: a value the new chart's schema rejects, or a
template that errors on this repository's values. A failure in another
release, or a source that could not be fetched, says nothing about the
change.

A render that succeeds is an offline approximation of what the cluster
applies. CRDs and Secrets are left out, `${SECRET_*}` values show as
`..PLACEHOLDER_SECRET_<name>..`, and templates that branch on Kubernetes
capabilities see flate's bundled version, not the cluster's. Within that,
take label values, resource names and ports from the render rather than
from a reading of the templates.
