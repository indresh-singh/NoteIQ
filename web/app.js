const $ = (selector) => document.querySelector(selector);
let token = "";
let inTeams = false;
let loading = false;
let teamsUserId = "";
let syncMessage = "";
let previousMeetings = "";
try { token = sessionStorage.getItem("noteiq-session") || ""; } catch { /* Memory-only fallback. */ }

function remember(value) {
  token = value;
  try { value ? sessionStorage.setItem("noteiq-session", value) : sessionStorage.removeItem("noteiq-session"); } catch {}
}

function showError(message = "") {
  $("#error").textContent = message;
  $("#error").hidden = !message;
}

function signedOut() {
  syncMessage = "";
  remember("");
  previousMeetings = "";
  $("#meetings").replaceChildren();
  $("#workspace").hidden = true;
  $("#welcome").hidden = false;
}

async function api(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
  const response = await fetch(path, {
    signal: controller.signal,
    method: body === undefined ? "GET" : "POST",
    headers: {"Content-Type": "application/json", ...(token ? {Authorization: `Bearer ${token}`} : {})},
    ...(body === undefined ? {} : {body: JSON.stringify(body)})
  });
  if (!response.ok) {
    if (response.status === 401) signedOut();
    let detail;
    try { detail = (await response.json()).detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : "NoteIQ couldn't complete this request. Please try again.");
  }
  return response.json();
  } catch (error) {
    if (error.name === "AbortError") throw new Error("The request timed out. Please try Refresh again.");
    throw error;
  } finally { clearTimeout(timeout); }
}

function popupResult(popup, url) {
  return new Promise((resolve, reject) => {
    const cleanup = () => { window.removeEventListener("message", receive); clearInterval(timer); };
    const receive = (event) => {
      if (event.origin !== location.origin || event.source !== popup || event.data?.type !== "noteiq-auth") return;
      cleanup();
      event.data.error ? reject(new Error(event.data.error)) : resolve(event.data.code);
    };
    window.addEventListener("message", receive);
    const started = Date.now();
    const timer = setInterval(() => {
      if (popup.closed || Date.now() - started > 600000) {
        cleanup(); reject(new Error("Sign-in window closed or expired. Please connect again."));
      }
    }, 500);
    popup.location.href = url;
  });
}

async function connect() {
  showError();
  $("#connect").disabled = true;
  // Open immediately on the click so ordinary browsers don't block the popup after an await.
  const popup = inTeams ? null : window.open("about:blank", "noteiq-signin", "width=600,height=650");
  try {
    if (!inTeams && !popup) throw new Error("Allow popups for NoteIQ, then connect again.");
    const bytes = crypto.getRandomValues(new Uint8Array(32));
    const verifier = btoa(String.fromCharCode(...bytes)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
    const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
    const challenge = Array.from(new Uint8Array(hash), (b) => b.toString(16).padStart(2, "0")).join("");
    const {url} = await api("/api/auth/start", {challenge, in_teams: inTeams});
    const code = inTeams
      ? await microsoftTeams.authentication.authenticate({url, width:600, height:650})
      : await popupResult(popup, url);
    const result = await api("/api/auth/complete", {code, verifier});
    remember(result.token);
    await refresh();
  } catch (error) { if (popup) popup.close(); showError(error.message || String(error)); }
  finally { $("#connect").disabled = false; }
}
$("#connect").onclick = () => connect();
$("#notification-retry").onclick = async () => {
  try { await api("/api/notifications/retry", {}); await refresh(); }
  catch (error) { showError(error.message); }
};

const statuses = {
  CONNECTING: "Connecting your meeting updates…",
  LISTENING: "Listening for transcripts and Copilot insights. Meeting access is checked when content is fetched.",
  ACCESS_REQUIRED: "Meeting access needs attention. Ask your administrator to check Copilot licensing, Graph consent and the application access policy.",
  CONNECTION_ERROR: "Unable to connect to Microsoft Graph. Check the server configuration and retry.",
  MISSED_EVENTS: "Some updates were missed while NoteIQ was offline. Your administrator can recover a meeting using its link."
};

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function renderNote(note) {
  const line = element("span");
  if (note.title) line.append(element("strong", note.title + ": "));
  line.append(document.createTextNode(note.text || ""));
  if (!note.subpoints?.length) {
    const paragraph = element("p");
    paragraph.append(line);
    return paragraph;
  }
  const detail = element("details");
  const summary = element("summary");
  summary.append(line);
  detail.append(summary, ...note.subpoints.map(renderNote));
  return detail;
}

function renderMeetings(meetings) {
  const signature = JSON.stringify(meetings);
  if (signature === previousMeetings) return;
  previousMeetings = signature;
  $("#meetings").replaceChildren();
  $("#empty").hidden = meetings.length > 0;
  for (const meeting of meetings) {
    const article = element("article", "", "meeting");
    article.append(element("p", "MEETING FOLLOW-UP", "eyebrow"), element("h2", meeting.subject));
    const date = meeting.content.insight?.endDateTime || meeting.content.transcript?.createdDateTime;
    if (date) article.append(element("time", new Date(date).toLocaleString()));
    article.append(element("p", meeting.content.insight ? "Copilot insights available" :
      "Transcript ready. Waiting for Copilot's summary and action items.", "hint"));
    const buttons = element("div", "", "buttons");
    const content = element("div", "", "card-content");
    for (const [label, heading] of [["Summary", "KEY NOTES"], ["Action items", "ACTION ITEMS"]]) {
      const button = element("button", label);
      button.setAttribute("aria-pressed", "false");
      button.onclick = () => {
        buttons.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
        content.replaceChildren();
        const segments = meeting.content.insights || (meeting.content.card ? [{card: meeting.content.card}] : []);
        for (const segment of segments) {
          const insight = segment.insight || meeting.content.insight;
          const items = heading === "KEY NOTES" ? insight?.meetingNotes : insight?.actionItems;
          if (items?.length) {
            content.append(element("h3", heading === "KEY NOTES" ? "Meeting notes" : "Follow-up tasks"));
            for (const item of items) {
              content.append(renderNote(item));
              if (heading === "ACTION ITEMS") content.append(element("p", item.ownerDisplayName || "Owner not specified", "hint"));
            }
          }
        }
        if (!content.children.length) content.append(element("p", meeting.content.insight ? "No items were included in the Copilot insights." : "Copilot insights aren't available yet."));
      };
      buttons.append(button);
    }
    const transcriptButton = element("button", "Transcripts");
    transcriptButton.setAttribute("aria-pressed", "false");
    transcriptButton.onclick = () => {
      buttons.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === transcriptButton)));
      content.replaceChildren();
      const transcripts = meeting.content.transcripts || [];
      if (!transcripts.length) content.append(element("p", "Transcript not synced yet. Use Refresh to check Microsoft 365; results appear automatically when ready."));
      for (const {transcript} of transcripts) {
        const detail = element("details");
        detail.append(element("summary", transcript.createdDateTime ? new Date(transcript.createdDateTime).toLocaleString() : "View transcript"));
        const text = element("pre", "Loading…", "transcript-text");
        detail.append(text);
        let loaded = false;
        detail.ontoggle = async () => {
          if (!detail.open || loaded) return;
          try {
            const response = await fetch(`/api/transcripts/${transcript.local_id}`, {headers:{Authorization:`Bearer ${token}`}});
            if (!response.ok) throw new Error("Unable to load transcript. Refresh or sign in again.");
            text.textContent = (await response.text()).replace(/<v ([^>]+)>/g, "$1: ").replaceAll("</v>", "");
            loaded = true;
          } catch (error) { text.textContent = error.message; }
        };
        content.append(detail);
      }
    };
    buttons.append(transcriptButton);
    article.append(buttons, content, element("p", "Generated from Microsoft 365 Copilot meeting insights.", "hint"));
    $("#meetings").append(article);
    buttons.firstChild.click();
  }
}

async function refresh(sync = false) {
  if (!token || loading) return;
  const startedWith = token;
  loading = true;
  $("#refresh").disabled = true;
  $("#refresh").textContent = sync ? "Checking Microsoft 365…" : "Refreshing…";
  try {
    const user = await api("/api/me");
    if (teamsUserId && user.id.toLowerCase() !== teamsUserId.toLowerCase()) {
      signedOut();
      throw new Error("NoteIQ was connected with a different Microsoft account. Connect again and choose the account you use in Teams.");
    }
    if (sync) {
      const result = await api("/api/sync", {});
      syncMessage = result.queued
        ? "Microsoft 365 check queued. Results update automatically as the check completes."
        : "No known meetings to check yet. New meetings appear when Microsoft sends their updates.";
    }
    const meetings = await api("/api/meetings");
    if (token !== startedWith) return;
    $("#welcome").hidden = true;
    $("#workspace").hidden = false;
    $("#greeting").textContent = `Welcome, ${user.name}`;
    $("#status").textContent = (statuses[user.status] || user.status) + (syncMessage ? " " + syncMessage : "");
    const notificationError = user.notifications === "DELIVERY_ERROR";
    $("#notification-status").textContent = notificationError
      ? "Teams notification delivery needs attention. Install or update NoteIQ in Teams and accept its notification permission, then retry."
      : "NoteIQ will notify you in Teams Activity when transcripts and insights are ready.";
    $("#notification-retry").hidden = !notificationError;
    $("#retry").hidden = !["ACCESS_REQUIRED", "CONNECTION_ERROR"].includes(user.status);
    renderMeetings(meetings);
    showError();
  } catch (error) { showError(error.message); }
  finally { loading = false; $("#refresh").disabled = false; $("#refresh").textContent = "Refresh"; }
}

$("#refresh").onclick = () => refresh(true);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
window.addEventListener("focus", () => refresh());
$("#retry").onclick = async () => {
  try { await api("/api/reconnect", {}); await refresh(); } catch (error) { showError(error.message); }
};
$("#signout").onclick = async () => {
  try { await api("/api/logout", {}); signedOut(); } catch (error) { showError(error.message); }
};
$("#disconnect").onclick = async () => {
  if (!confirm("Stop collecting meeting notes and delete the cards saved in NoteIQ?")) return;
  try { await api("/api/disconnect", {}); signedOut(); } catch (error) { showError(error.message); }
};

(async () => {
  $("#connect").disabled = true;
  try {
    await Promise.race([
      microsoftTeams.app.initialize(),
      new Promise((_, reject) => setTimeout(() => reject(new Error("Not running in Teams.")), 5000))
    ]);
    inTeams = true;
    const theme = (value) => { document.body.className = value === "default" ? "" : value; previousMeetings = ""; refresh(); };
    const context = await microsoftTeams.app.getContext();
    teamsUserId = context.user?.id || "";
    theme(context.app.theme);
    microsoftTeams.app.registerOnThemeChangeHandler(theme);
    microsoftTeams.app.notifyAppLoaded();
    microsoftTeams.app.notifySuccess();
  } catch { /* The same page works directly in a browser. */ }
  $("#connect").disabled = false;
  await refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 15000);
})();
