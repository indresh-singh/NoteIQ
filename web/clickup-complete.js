(async () => {
  const result = document.body;
  const error = result.dataset.error || "";
  if (result.dataset.inTeams !== "true") {
    if (window.opener) {
      window.opener.postMessage({type: "noteiq-clickup", error}, location.origin);
      window.close();
    }
    return;
  }
  try {
    await microsoftTeams.app.initialize();
    if (error) microsoftTeams.authentication.notifyFailure(error);
    else microsoftTeams.authentication.notifySuccess("clickup-connected");
  } catch {
    document.body.textContent = "Couldn't return to Teams. Close this window and try again.";
  }
})();
