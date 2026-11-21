#!/usr/bin/env bash
# shopify-nextjs checks. Exit 0 only when the branch is fit to review.
#
# factory/bin/gate runs this script from the controller checkout with the job's
# worktree as the working directory. Non-interactive, never rewrites source
# (no --fix), and any failing command fails the gate.
#
# The repo has no CI workflow and no test runner; these are its package.json
# scripts: `lint` (next lint) and `build` (next build). `next build` prerenders
# pages from the Shopify Storefront API, so it needs SHOPIFY_STORE_DOMAIN and
# SHOPIFY_STOREFRONT_ACCESSTOKEN (the gate passes them from the controller's
# .env.local; see checks_env in factory/config.json) and network access.
set -euo pipefail
export NEXT_TELEMETRY_DISABLED=1

npm ci --no-audit --no-fund --prefer-offline
npm run lint

if [ -z "${SHOPIFY_STORE_DOMAIN:-}" ] || [ -z "${SHOPIFY_STOREFRONT_ACCESSTOKEN:-}" ]; then
  echo "checks.sh: SHOPIFY_STORE_DOMAIN and SHOPIFY_STOREFRONT_ACCESSTOKEN are not set;" \
       "'next build' cannot prerender Shopify pages without them." >&2
  exit 1
fi
npm run build
