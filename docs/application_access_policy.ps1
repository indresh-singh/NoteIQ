# Stop if any command fails.
$ErrorActionPreference = "Stop"

# Your Microsoft 365 directory — copy this from AZURE_TENANT_ID in .env
# (the single source of truth); do not hardcode it here.
$tenantId = ""

# The CURRENT NoteIQ application client ID — copy this from GRAPH_CLIENT_ID in .env
# (the single source of truth); do not hardcode it here.
$appId = ""

# Praveen Singh's user Object ID — the user receiving meeting access.
$userId = "e13f5eaa-e406-4880-95e4-3f7e2f44096a"

# The policy you already created.
$policyName = "NoteIQ-Pilot"

# Load the Teams administration commands.
Import-Module MicrosoftTeams

# Sign in as an administrator of your tenant.
Connect-MicrosoftTeams -TenantId $tenantId -UseDeviceAuthentication

# Add the current app ID while preserving other apps in this policy.
Set-CsApplicationAccessPolicy -Identity $policyName -AppIds @{Add = $appId}

# Assign this policy to Praveen.
Grant-CsApplicationAccessPolicy -Identity $userId -PolicyName $policyName

# Verify that the policy contains the current app ID.
Get-CsApplicationAccessPolicy -Identity $policyName

# Verify Praveen's policy assignment.
Get-CsOnlineUser -Identity $userId |
    Select-Object DisplayName, UserPrincipalName, ApplicationAccessPolicy