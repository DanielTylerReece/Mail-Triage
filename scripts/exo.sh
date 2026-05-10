#!/usr/bin/env bash
# Linux-only Exchange Online operations via pwsh-in-Docker.
#
# Microsoft's ExchangeOnlineManagement PowerShell module is the ONLY
# supported way to create an Application Access Policy or to manage the
# scope group; there is no Graph API equivalent. PowerShell Core (pwsh)
# runs natively on Linux via Microsoft's official Docker image, so we
# don't need a Windows host — this script wraps that image.
#
# A persistent docker volume holds the installed ExchangeOnlineManagement
# module so we don't re-install it every run.
#
# All sign-in is via OAuth device code: pwsh prints a URL + code, you
# open it in your browser on any device and sign in. No persistent state.
#
# Usage:
#   ./scripts/exo.sh setup    -- one-time: create group + access policy
#   ./scripts/exo.sh sync     -- reconcile group membership to mailboxes.txt
#   ./scripts/exo.sh verify   -- prove the policy is in effect
#   ./scripts/exo.sh shell    -- interactive pwsh with EXO loaded
#
# Required env (set in .env or shell):
#   TENANT_ID            Entra tenant ID (used for Connect-ExchangeOnline)
#   EXO_GROUP_ADDRESS    Primary SMTP of the scope group, e.g.
#                        mailtriage-scope@your-tenant.onmicrosoft.com
#   EXO_APP_ID           Entra app (client) ID
#   EXO_CONTROL_MAILBOX  A non-target mailbox used to verify the policy
#                        actually denies (e.g. someone-else@your-tenant)

set -euo pipefail

# Locate repo root regardless of where the script is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Source .env so EXO_* vars are available.
if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.env"
  set +a
fi

: "${TENANT_ID:?TENANT_ID must be set in .env or the environment}"

PWSH_IMAGE="${PWSH_IMAGE:-mcr.microsoft.com/powershell:lts-debian-12}"
EXO_VOL="${EXO_VOL:-mailtriage-exo-modules}"

ACTION="${1:-}"; shift || true

run_pwsh() {
  # -i so device-code prompt is interactive; -t for terminal colors
  docker run --rm -it \
    -v "$EXO_VOL:/root/.local/share/powershell" \
    -v "$REPO_ROOT/scripts:/scripts:ro" \
    -v "$REPO_ROOT/config:/config:ro" \
    -e TENANT_ID \
    -e EXO_GROUP_ADDRESS \
    -e EXO_APP_ID \
    -e EXO_CONTROL_MAILBOX \
    "$PWSH_IMAGE" \
    pwsh -NoLogo -Command "$@"
}

ensure_module='
if (-not (Get-Module -ListAvailable ExchangeOnlineManagement)) {
  Write-Host "Installing ExchangeOnlineManagement (one-time)..." -ForegroundColor Cyan
  Install-Module -Name ExchangeOnlineManagement -Force -Scope CurrentUser -AcceptLicense
}
Import-Module ExchangeOnlineManagement
Connect-ExchangeOnline -Device -ShowBanner:$false
'

case "$ACTION" in
  setup)
    : "${EXO_GROUP_ADDRESS:?EXO_GROUP_ADDRESS must be set}"
    : "${EXO_APP_ID:?EXO_APP_ID must be set}"
    run_pwsh "$ensure_module
& /scripts/exo-setup.ps1 -GroupAddress \"\$env:EXO_GROUP_ADDRESS\" -AppId \"\$env:EXO_APP_ID\""
    ;;

  sync)
    : "${EXO_GROUP_ADDRESS:?EXO_GROUP_ADDRESS must be set}"
    run_pwsh "$ensure_module
& /scripts/sync-scope.ps1 -GroupAddress \"\$env:EXO_GROUP_ADDRESS\" -MailboxesFile /config/mailboxes.txt $*"
    ;;

  verify)
    : "${EXO_APP_ID:?EXO_APP_ID must be set}"
    : "${EXO_CONTROL_MAILBOX:?EXO_CONTROL_MAILBOX must be set}"
    run_pwsh "$ensure_module
& /scripts/exo-verify.ps1 -AppId \"\$env:EXO_APP_ID\" -MailboxesFile /config/mailboxes.txt -ControlMailbox \"\$env:EXO_CONTROL_MAILBOX\""
    ;;

  shell)
    run_pwsh "$ensure_module
Write-Host \"Connected. ExchangeOnlineManagement is loaded. Type 'exit' when done.\" -ForegroundColor Green"
    ;;

  ""|help|-h|--help)
    sed -n '/^# /,/^$/p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;

  *)
    echo "Unknown action: $ACTION" >&2
    echo "Run '$0 help' for usage." >&2
    exit 1
    ;;
esac
