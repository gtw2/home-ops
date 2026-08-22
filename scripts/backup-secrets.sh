#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-/home/greg/ownCloud/Personal/Backup/github/homeops}"

# Relative paths (from repo root) of the ignored files to back up.
FILES=(
  age.key
  kubeconfig
  cloudflare-tunnel.json
  github-deploy.key
  github-deploy.key.pub
  talos/clusterconfig/talosconfig
)

mkdir -p "$DEST"
echo "Backing up secrets from $REPO_DIR -> $DEST"

for rel in "${FILES[@]}"; do
  src="$REPO_DIR/$rel"
  if [[ ! -e "$src" ]]; then
    printf '  skip (missing): %s\n' "$rel"
    continue
  fi
  # Preserve subdirectory structure (e.g. talos/clusterconfig/talosconfig).
  dst="$DEST/$rel"
  mkdir -p "$(dirname "$dst")"
  if cmp -s "$src" "$dst" 2>/dev/null; then
    printf '  ok (unchanged): %s\n' "$rel"
  else
    cp -p "$src" "$dst"
    printf '  copied:         %s\n' "$rel"
  fi
done

echo "Done."
