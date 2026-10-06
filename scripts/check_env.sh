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
# Read the provider actually configured rather than assuming one -- a
# checklist that reports on the wrong provider is worse than no checklist.
PROVIDER="$(grep -A6 '^ *email:' harvey.local.yaml harvey.yaml 2>/dev/null \
    | grep -m1 'provider:' | sed 's/.*provider: *//; s/["'"'"']//g; s/ *#.*//' | tr -d '\r')"
PROVIDER="${PROVIDER:-unknown}"
echo " Mail provider (configured: $PROVIDER):"
case "$PROVIDER" in
    gmail)
        have GMAIL_CLIENT_ID
        have GMAIL_CLIENT_SECRET
        echo "      also run 'harvey gmail auth' once, locally"
        ;;
    smtp)
        have SMTP_HOST
        have SMTP_USERNAME
        have SMTP_PASSWORD
        have IMAP_HOST
        ;;
    instantly)
        have INSTANTLY_API_KEY
        ;;
    *)
        echo "  [ ] channels.email.provider is not gmail, smtp or instantly"
        ;;
esac
echo "      verify with: harvey mail test"
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
if [ "$PROVIDER" != "instantly" ]; then
    echo "NOT NEEDED (provider is $PROVIDER, not instantly)"
    echo
    if [ -n "${INSTANTLY_API_KEY:-}" ]; then
        echo "  INSTANTLY_API_KEY is set but unused"
    else
        echo "  INSTANTLY_API_KEY unset - correct"
    fi
    echo
fi
