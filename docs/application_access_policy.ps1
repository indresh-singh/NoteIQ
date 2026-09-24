$TenantId  = ""   # AZURE_TENANT_ID from .env.dev or .env.prod
$AppId     = ""   # GRAPH_CLIENT_ID from .env.dev or .env.prod
$UserId    = ""   # user's object ID

$PolicyName = "NoteIQ-Pilot"

Import-Module MicrosoftTeams
Connect-MicrosoftTeams -TenantId $TenantId

if (Get-CsApplicationAccessPolicy -Identity $PolicyName -ErrorAction SilentlyContinue) {
    Set-CsApplicationAccessPolicy -Identity $PolicyName -AppIds @{Add = $AppId}
} else {
    New-CsApplicationAccessPolicy -Identity $PolicyName -AppIds @($AppId) -Description "NoteIQ meeting insights pilot"
}
Grant-CsApplicationAccessPolicy -Identity $UserId -PolicyName $PolicyName

Get-CsOnlineUser -Identity $UserId | Select-Object DisplayName, UserPrincipalName, ApplicationAccessPolicy
