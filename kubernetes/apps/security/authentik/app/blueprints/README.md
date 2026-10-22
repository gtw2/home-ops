# authentik blueprints

Authentik's configuration as code. Each `*.yaml` here becomes a key of the
`authentik-blueprints` ConfigMap (`../kustomization.yaml`), which the chart
mounts into the worker at `/blueprints/mounted/cm-authentik-blueprints/`
(`blueprints.configMaps` in `../helmrelease.yaml`). The worker discovers the
files on change and hourly. Each one gets a BlueprintInstance, and upstream
re-applies instances on a schedule, so an edit made in the UI to an object a
blueprint manages does not survive.

Schema and examples are upstream's at `version/<chart tag>`, under
`blueprints/` in goauthentik/authentik. `system/` and `default/` there are
what a fresh instance applies on its own. Read the default flows before
writing your own.

## Why blueprints and not the Terraform provider

The `goauthentik/authentik` Terraform provider covers the same objects, but it
needs a state backend, an API token held outside the cluster, and something to
run `apply`. That is a second control loop beside Flux, and on a rebuild it
can only run after authentik is up. Blueprints are applied by authentik itself
from files Flux already ships. They can't do a plan/diff, which the adoption
step below works around.

## What a rebuild restores, and from where

| what | source |
|---|---|
| users, passwords, MFA devices, sessions, invitations | the `authentik` database, restored by CloudNative-PG (barman-cloud) |
| groups, flows, stages, providers, applications, bindings | these blueprints |
| OIDC client IDs and secrets | 1Password, each app's own item, through `!Env` |
| the first admin and an API token | `AUTHENTIK_BOOTSTRAP_*`, commented out in `../externalsecret.yaml` |

If the database comes back, blueprints just re-assert what is already there.
If it doesn't, blueprints rebuild every app's SSO with the credentials the
apps already hold. People have to enroll again, but nothing has to be
re-wired on the app side.

## Conventions

- **Identify by the stable thing.** Providers by `client_id`, applications by
  `slug`, scope mappings by `scope_name`, everything else by `name`. A blueprint
  that matches an existing object updates it. One that doesn't match creates a
  second copy, which for a provider means a second client_id.
- **Secrets only through `!Env`.** The ConfigMap is plain text. Add a
  `BLUEPRINT_*` key to `../externalsecret.yaml` from the app's own 1Password
  item, the fields its ExternalSecret already reads.
- **Leave out what you have not read from the live instance.** An attribute that
  is absent keeps the live value on update and takes the default on create.
  `sub_mode` is the dangerous one: change it and every user's subject changes.
- **`${SECRET_DOMAIN}` works.** Flux substitutes the ConfigMap like any
  other manifest.

## Files

| file | owns |
|---|---|
| `groups.yaml` | every non-system group, without members |
| `access.yaml` | bindings for the applications that have no blueprint yet |
| `friends.yaml` | the friends invitation flow |
| `open-webui.yaml`, `litellm.yaml` | provider, application and bindings, adopted from the live instance |

**Who gets in.** Until `access.yaml`, no application had a policy binding, so
every active user could log in everywhere. Now every application admits
`authentik Admins` and `authentik Users`, which covers every account that
existed on 2026-10-22. Open WebUI also admits `friends`, and nothing else does.
So the rule is: household goes in `authentik Users`, friends go in `friends`,
and never both.

**Two invitation flows.** `enrollment-invitation` (hand-made) puts new users in
`authentik Users`, so it's for household. `friends-enrollment` puts them in
`friends`.

## Adopting what exists

A blueprint that matches a live object overwrites whatever differs, and
blueprints can't show a diff. To adopt an object, read it from the instance
first. Add `blueprints.goauthentik.io/instantiate: "false"` to the metadata
labels to ship a file without applying it. The inventory scripts used for
`open-webui.yaml` and `litellm.yaml` printed, per OAuth2 provider: flows,
signing key, `sub_mode`, `issuer_mode`, redirect URIs and property mappings.
Copy those exactly, and never change `sub_mode` on a live provider.

## Not yet here

Still made by hand in the UI. Bindings for each are already in `access.yaml`.
When you port one, move its bindings into its own file.

| application | note |
|---|---|
| grafana | |
| immich | explicit consent, `authentication_flow` set, an `app.immich://` redirect |
| kritika | `client_id` is literally `kritika`, not a 1Password value |
| nextcloud | |
| ocis-web, -desktop, -android, -ios | web is a public client; desktop uses regex redirects; the mobile ones take `offline_access` |
| romm | uses the custom `OAuth Mapping: OpenID 'email-custom'` (`email_verified: True`) instead of the default email scope |
| scrob, sparky | |

Also still in the UI: the brand, the `default-authentication-flow` with its
MFA stage, the `google` source, and the `enrollment`, `enrollment-invitation`
and `default-recovery` flows.

Seen in the 2026-10-22 inventory and left alone:
- **`Home forward auth`** is a proxy provider with no application and no
  outpost. It's unused, so it can be deleted.
- **The `google` source** enrolls through `default-source-enrollment`, whose
  write stage creates users *inactive* in `authentik Users`. So a stranger who
  signs in with Google gets an account that can't log in until someone
  activates it. Activating one gives it household access.
- **`default/flow-default-authenticator-webauthn-setup.yaml`** reports
  `error` status.
