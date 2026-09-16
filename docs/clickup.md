# ClickUp action-item export

NoteIQ sends Copilot action items to ClickUp only after the user chooses **Send
action items to ClickUp**. It creates tasks in that user's selected List. A second
click skips tasks that NoteIQ already created for that action item.

## One-time ClickUp setup

1. In ClickUp, go to **Settings → Apps → Create new app**. You must be a
   Workspace owner or admin.
2. Set the redirect URL to:

   ```text
   https://noteiq.salmontree-16ed39aa.uaenorth.azurecontainerapps.io/clickup/callback
   ```

3. Copy the ClickUp app's client ID and secret.
4. Generate a NoteIQ encryption key once, locally:

   ```bash
   uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   ```

5. In Azure Portal → Container Apps → **noteiq** → Settings → Secrets, add
   `clickup-client-secret` (the ClickUp secret) and `clickup-token-key` (the
   generated Fernet key).
6. Add these container environment variables:

   ```text
   CLICKUP_CLIENT_ID=<ClickUp client ID>
   CLICKUP_CLIENT_SECRET=secretref:clickup-client-secret
   CLICKUP_TOKEN_KEY=secretref:clickup-token-key
   ```

Keep `CLICKUP_TOKEN_KEY` unchanged. It encrypts each user's ClickUp access token
in NoteIQ's database; changing it means users need to reconnect ClickUp.

After deployment, open NoteIQ → **Account settings** → **Connect ClickUp**. Then
paste the target List ID. In ClickUp, copy a List link; its ID is the value after
`/li/` in that URL.

ClickUp's OAuth flow is appropriate for a multi-user integration. Its API uses
`POST /api/v2/list/{list_id}/task` to create each task. See [ClickUp OAuth](https://developer.clickup.com/docs/authentication) and [Create Task](https://developer.clickup.com/reference/createtask).
