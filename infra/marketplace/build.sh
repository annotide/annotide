#!/usr/bin/env bash
# Assemble the Azure Marketplace bundle source in infra/marketplace/build/:
# the Helm chart with this folder's values.yaml merged over its own, the ARM
# template, the install form and the manifest. container-package-app (cpa)
# then verifies it and builds the CNAB (README.md).
#
#   infra/marketplace/build.sh 0.3.0 annotidemarketplace.azurecr.io
#
# Needs yq v4. The version is the release (images ghcr.io/annotide/*:<version>).
set -euo pipefail

version=${1:?usage: build.sh <version, e.g. 0.3.0> [registry server]}
registry=${2:-set-by-build.azurecr.io}
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must be #.#.#, got $version" >&2; exit 1; }

here=$(cd "$(dirname "$0")" && pwd)
chart="$here/../helm/annotide"
out="$here/build"

rm -rf "$out"
mkdir -p "$out"
cp -R "$chart" "$out/annotide"
# CI-only value files are not part of the product.
rm -rf "$out/annotide/ci"

export VERSION="$version" REGISTRY="$registry"
yq -i '.version = strenv(VERSION) | .appVersion = strenv(VERSION)' "$out/annotide/Chart.yaml"
# Deep merge: the bundle's values over the chart's.
yq eval-all -i 'select(fileIndex == 0) * select(fileIndex == 1)' \
  "$out/annotide/values.yaml" "$here/values.yaml"
yq -i '.global.azure.images.backend.tag = strenv(VERSION) | .global.azure.images.frontend.tag = strenv(VERSION)' \
  "$out/annotide/values.yaml"

cp "$here/mainTemplate.json" "$here/createUIDefinition.json" "$out/"
yq '.version = strenv(VERSION) | .registryServer = strenv(REGISTRY)' "$here/manifest.yaml" > "$out/manifest.yaml"

echo "bundle source for $version in $out"
