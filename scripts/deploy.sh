#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Deploy the committed tree (HEAD) to a host over SSH and run the installer.
#   scripts/deploy.sh <ssh-host> [install|upgrade|uninstall] [installer options]
# Uncommitted changes are not deployed: git archive packs HEAD only.
set -eu
host=${1:?usage: deploy.sh <ssh-host> [install|upgrade|uninstall] [options]}
shift
action=${1:-upgrade}
[ $# -gt 0 ] && shift
cd "$(dirname "$0")/.."
if [ -n "$(git status --porcelain)" ]; then
    echo "deploy: note: working tree has uncommitted changes; deploying HEAD $(git rev-parse --short HEAD) only" >&2
fi
git archive --format=tar HEAD | ssh "$host" "set -e; d=\$(mktemp -d); trap 'rm -rf \$d' EXIT; tar -x -C \$d; \$d/install/install.sh $action $*"
