# One-time Exchange Online setup for mailtriage.
#
# Creates the (initially empty) scope security group AND attaches the
# Application Access Policy to it. Idempotent — re-running with the
# same params is safe.
#
# Run via the wrapper: ./scripts/exo.sh setup

param(
  [Parameter(Mandatory)] [string] $GroupAddress,
  [Parameter(Mandatory)] [string] $AppId
)

# ----- 1. Group -----

$existing = $null
try {
  $existing = Get-DistributionGroup -Identity $GroupAddress -ErrorAction Stop
} catch {
  # not found
}

if ($existing) {
  Write-Host "Group already exists: $GroupAddress" -ForegroundColor Cyan
} else {
  $name = ($GroupAddress -split '@')[0]
  New-DistributionGroup `
    -Name $name `
    -Type "Security" `
    -PrimarySmtpAddress $GroupAddress | Out-Null
  Write-Host "Created group: $GroupAddress" -ForegroundColor Green
}

# ----- 2. Application Access Policy -----

$existingPolicy = Get-ApplicationAccessPolicy -ErrorAction SilentlyContinue |
  Where-Object { $_.AppId -eq $AppId -and $_.ScopeIdentity -eq $GroupAddress }

if ($existingPolicy) {
  Write-Host "Application Access Policy already exists for app $AppId targeting $GroupAddress" -ForegroundColor Cyan
} else {
  New-ApplicationAccessPolicy `
    -AppId $AppId `
    -PolicyScopeGroupId $GroupAddress `
    -AccessRight RestrictAccess `
    -Description "Limit mailtriage app to mailboxes in $GroupAddress" | Out-Null
  Write-Host "Created Application Access Policy" -ForegroundColor Green
}

Write-Host ""
Write-Host "Done. Next:" -ForegroundColor Cyan
Write-Host "  1. Wait up to 60 minutes for policy propagation."
Write-Host "  2. Edit config/mailboxes.txt with your real mailboxes."
Write-Host "  3. Run: ./scripts/exo.sh sync"
Write-Host "  4. Run: ./scripts/exo.sh verify"
