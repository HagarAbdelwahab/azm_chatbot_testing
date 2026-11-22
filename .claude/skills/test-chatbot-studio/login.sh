#!/usr/bin/env bash
# Log in to Agent Studio and scope the session to one tenant, printing ONE status line.
#
#   .claude/skills/test-chatbot-studio/login.sh [tenant-name]     # default Tarekat-Testing
#
# Why a script: done by hand, every playwright-cli step prints the Playwright code it ran,
# and `fill` echoes the typed value, so the email AND PASSWORD land in the agent's
# context. Here all playwright-cli output goes to /dev/null, and the credentials come
# from .env, never from the command line.
#
# Run from the repo root: playwright-cli keeps its session in ./.playwright-cli/, and
# run_tests.sh must later run from the same directory.
set -uo pipefail

TENANT="${1:-Tarekat-Testing}"
SESSION="${STUDIO_SESSION:-studio}"
[ -f .env ] && { set -a; . ./.env; set +a; }
: "${STUDIO_URL:?STUDIO_URL missing from .env}"
: "${STUDIO_EMAIL:?STUDIO_EMAIL missing from .env}"
: "${STUDIO_PASSWORD:?STUDIO_PASSWORD missing from .env}"

pc() { playwright-cli -s="$SESSION" "$@" >/dev/null 2>&1; }
ev() { playwright-cli -s="$SESSION" --raw eval "$1" 2>/dev/null | tail -1 | tr -d '"'; }

# Wait (up to N x 1s) until a JS predicate returns "1".
wait_for() {
  local js="$1" n="${2:-20}"
  for _ in $(seq 1 "$n"); do
    [ "$(ev "$js")" = "1" ] && return 0
    sleep 1
  done
  return 1
}

pc open "$STUDIO_URL/login"
wait_for "() => document.querySelector('input[type=password]') ? 1 : 0" \
  || { echo "LOGIN FAILED: login form not found at $STUDIO_URL/login"; exit 1; }
pc fill "input[type=email], input[name=email]" "$STUDIO_EMAIL"
pc fill "input[type=password]" "$STUDIO_PASSWORD"
pc click "button:has-text('Sign In')"
wait_for "() => location.pathname.startsWith('/dashboard') ? 1 : 0" \
  || { echo "LOGIN FAILED: did not reach /dashboard (wrong credentials or the page changed)"; exit 1; }

# The account is a super-admin with no tenant: until one is picked, agent pages are
# blank and the API 404s on valid ids. The switcher renders "Loading…" at first.
pc goto "$STUDIO_URL/dashboard/tenants"
wait_for "() => [...document.querySelectorAll('button')].some(b => /select tenant|Tarekat|Insaf|test_tenant/.test(b.textContent) && !/Loading/.test(b.textContent)) ? 1 : 0" \
  || { echo "TENANT FAILED: tenant switcher never finished loading"; exit 1; }
pc click "button:has-text('— select tenant —')"
pc click "button:has-text('$TENANT')"
active=$(ev "() => [...document.querySelectorAll('p')].find(p => /Active tenant/.test(p.textContent))?.nextElementSibling?.textContent.trim() || ''")
if [ "$active" != "$TENANT" ]; then
  echo "TENANT FAILED: active tenant is '${active:-none}', wanted '$TENANT'"
  exit 1
fi
echo "studio   logged in · active tenant $TENANT · session '$SESSION'"
