#!/usr/bin/env bash
#
# What is configured, and what is missing. Reads the environment only --
# no network calls, no secrets printed, safe to run anywhere.
#
#   bash scripts/check_env.sh

have() { [ -n "${!1:-}" ] && echo "  [x] $1" || echo "  [ ] $1   <-- MISSING"; }
any()  { for v in "$@"; do [ -n "${!v:-}" ] && return 0; done; return 1; }

echo
echo "REQUIRED"
echo
echo " Cloud state:"
have HARVEY_STATE_REPO
echo
echo " Mail provider (gmail):"
have GMAIL_CLIENT_ID
have GMAIL_CLIENT_SECRET
echo
echo " Email verification -- without ANY of these every address stays"
echo " 'guess' and Harvey never sends a single email:"
if any REOON_API_KEY ZEROBOUNCE_API_KEY HUNTER_API_KEY; then
    echo "  [x] at least one verifier present"
else
    echo "  [ ] NONE SET  <-- the pipeline will run and send nothing"
fi
have REOON_API_KEY
have ZEROBOUNCE_API_KEY
have HUNTER_API_KEY
echo
echo " Web search -- Google rate-limits datacenter IPs on the first"
echo " request, so free search is close to useless from the cloud:"
have SERPER_API_KEY
echo
echo "OPTIONAL"
echo
have DATAFORSEO_LOGIN
have DATAFORSEO_PASSWORD
have CLOUDFLARE_ACCOUNT_ID
have CLOUDFLARE_API_TOKEN
have LINKEDIN_EMAIL
have LINKEDIN_PASSWORD
echo
echo "NOT NEEDED (provider is gmail, not instantly)"
echo
[ -n "${INSTANTLY_API_KEY:-}" ] && echo "  INSTANTLY_API_KEY is set but unused" || echo "  INSTANTLY_API_KEY unset - correct"
echo
