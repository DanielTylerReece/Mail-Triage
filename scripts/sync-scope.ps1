# Reconcile the mailtriage Application Access Policy scope group to match
# the contents of config/mailboxes.txt. Run from a Windows admin host
# whenever you add or remove a mailbox.
#
# The script:
#   - reads mailboxes.txt (one address per line; '#' comments allowed)
#   - reads current group membership
#   - adds members listed in the file but missing from the group
#   - removes members in the group but missing from the file
#   - leaves the rest alone
#
# Requires: ExchangeOnlineManagement module, Connect-ExchangeOnline already run.
#
# Usage:
#   .\sync-scope.ps1 -GroupAddress "mailtriage-scope@your-tenant.onmicrosoft.com" `
#                    -MailboxesFile .\mailboxes.txt
#
# Optional flags:
#   -DryRun       Print what would change, do nothing.
#   -NoConfirm    Skip the per-removal prompt.

param(
  [Parameter(Mandatory)] [string] $GroupAddress,
  [Parameter(Mandatory)] [string] $MailboxesFile,
  [switch] $DryRun,
  [switch] $NoConfirm
)

if (-not (Get-Command Get-DistributionGroupMember -ErrorAction SilentlyContinue)) {
  Write-Error "Run inside an Exchange Online PowerShell session: Connect-ExchangeOnline"
  exit 1
}
if (-not (Test-Path $MailboxesFile)) {
  Write-Error "Mailboxes file not found: $MailboxesFile"
  exit 1
}

# Parse mailboxes.txt: lower-cased, comments and blanks ignored.
$desired = @(
  Get-Content $MailboxesFile |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -and -not $_.StartsWith('#') -and $_.Contains('@') } |
    ForEach-Object { $_.ToLowerInvariant() }
) | Sort-Object -Unique

if ($desired.Count -eq 0) {
  Write-Error "No mailbox addresses parsed from $MailboxesFile"
  exit 1
}

# Current group membership: PrimarySmtpAddress lower-cased.
$current = @(
  Get-DistributionGroupMember -Identity $GroupAddress -ResultSize Unlimited |
    ForEach-Object { $_.PrimarySmtpAddress.ToString().ToLowerInvariant() }
) | Sort-Object -Unique

$toAdd    = @($desired | Where-Object { $_ -notin $current })
$toRemove = @($current | Where-Object { $_ -notin $desired })

Write-Host ""
Write-Host "Group        : $GroupAddress"
Write-Host "Source file  : $MailboxesFile"
Write-Host ("Desired      : {0} mailboxes" -f $desired.Count)
Write-Host ("Currently in : {0} mailboxes" -f $current.Count)
Write-Host ("To add       : {0}" -f $toAdd.Count)
Write-Host ("To remove    : {0}" -f $toRemove.Count)
Write-Host ""

foreach ($m in $toAdd)    { Write-Host "  + $m" -ForegroundColor Green }
foreach ($m in $toRemove) { Write-Host "  - $m" -ForegroundColor Yellow }

if ($toAdd.Count -eq 0 -and $toRemove.Count -eq 0) {
  Write-Host "No changes needed." -ForegroundColor Cyan
  exit 0
}

if ($DryRun) {
  Write-Host "`n--DryRun: no changes made.`n" -ForegroundColor Cyan
  exit 0
}

Write-Host ""
foreach ($m in $toAdd) {
  try {
    Add-DistributionGroupMember -Identity $GroupAddress -Member $m -ErrorAction Stop
    Write-Host "added $m" -ForegroundColor Green
  } catch {
    Write-Host "FAILED to add ${m}: $_" -ForegroundColor Red
  }
}

foreach ($m in $toRemove) {
  $confirm = if ($NoConfirm) { $true } else {
    $resp = Read-Host "Remove $m from group? [y/N]"
    $resp -match '^[Yy]'
  }
  if ($confirm) {
    try {
      Remove-DistributionGroupMember -Identity $GroupAddress -Member $m -Confirm:$false -ErrorAction Stop
      Write-Host "removed $m" -ForegroundColor Yellow
    } catch {
      Write-Host "FAILED to remove ${m}: $_" -ForegroundColor Red
    }
  } else {
    Write-Host "skipped $m" -ForegroundColor DarkGray
  }
}

Write-Host "`nDone. Re-run scripts\test-app-access-policy.ps1 if you added or removed addresses to verify the policy still matches.`n" -ForegroundColor Cyan
