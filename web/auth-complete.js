/* The result is a short-lived code bound to the initiating tab, never a Microsoft token. */
(async () => {
  const result = document.querySelector("#result");
  const {code, error} = result.dataset;
  document.querySelector("#message").textContent = error || "Connected. You can return to NoteIQ.";
  if (result.dataset.inTeams !== "true") {
    if (window.opener) {
      window.opener.postMessage({type: "noteiq-auth", code, error}, window.location.origin);
      window.close();
    }
    return;
  }
  try {
    await Promise.race([
      microsoftTeams.app.initialize(),
      new Promise((_, reject) => setTimeout(() => reject(new Error("Teams did not respond.")), 10000))
    ]);
    if (error) microsoftTeams.authentication.notifyFailure(error);
    else microsoftTeams.authentication.notifySuccess(code);
  } catch {
    document.querySelector("#message").textContent = "Couldn't return to Teams. Close this window and try connecting again.";
  }
})();
