# Verify the Application Access Policy is actually in effect.
#
# Reads the configured mailbox list from config/mailboxes.txt and
# confirms the access policy reports Granted for each, plus Denied
# (or RestrictedByPolicy) for the operator-supplied control mailbox.
#
# Run via the wrapper: ./scripts/exo.sh verify

param(
  [Parameter(Mandatory)] [string] $AppId,
  [Parameter(Mandatory)] [string] $MailboxesFile,
  [Parameter(Mandatory)] [string] $ControlMailbox
)

if (-not (Get-Command Test-ApplicationAccessPolicy -ErrorAction SilentlyContinue)) {
  Write-Error "Run inside an Exchange Online PowerShell session (use ./scripts/exo.sh)"
  exit 1
}
if (-not (Test-Path $MailboxesFile)) {
  Write-Error "Mailboxes file not found: $MailboxesFile"
  exit 1
}

$desired = @(
  Get-Content $MailboxesFile |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -and -not $_.StartsWith('#') -and $_.Contains('@') }
) | Sort-Object -Unique

if ($desired.Count -eq 0) {
  Write-Error "No mailbox addresses parsed from $MailboxesFile"
  exit 1
}

$failed = $false

foreach ($m in $desired) {
  $r = Test-ApplicationAccessPolicy -Identity $m -AppId $AppId
  if ($r.AccessCheckResult -ne 'Granted') {
    Write-Host "FAIL: $m should be GRANTED but got $($r.AccessCheckResult)" -ForegroundColor Red
    $failed = $true
  } else {
    Write-Host "OK:   $m granted" -ForegroundColor Green
  }
}

$r = Test-ApplicationAccessPolicy -Identity $ControlMailbox -AppId $AppId
if ($r.AccessCheckResult -eq 'Granted') {
  Write-Host "FAIL: $ControlMailbox is GRANTED — policy is not restricting access!" -ForegroundColor Red
  $failed = $true
} else {
  Write-Host "OK:   $ControlMailbox correctly DENIED ($($r.AccessCheckResult))" -ForegroundColor Green
}

if ($failed) {
  Write-Host "`nVerification failed. Note: policy propagation can take up to 60 minutes after a sync.`n" -ForegroundColor Red
  exit 1
} else {
  Write-Host "`nAll checks passed.`n" -ForegroundColor Cyan
}
