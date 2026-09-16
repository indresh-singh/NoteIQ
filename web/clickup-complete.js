const result = document.body;
if (window.opener) {
  window.opener.postMessage({type: "noteiq-clickup", error: result.dataset.error || ""}, location.origin);
}
window.close();
