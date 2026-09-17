const $ = (selector) => document.querySelector(selector);
function hasActionItems(content) {
  const insights = content.insights || (content.insight ? [{insight: content.insight}] : []);
  return insights.some((item) => (item.insight?.actionItems || []).some((a) => a.title || a.text));
}
let token = "";
let inTeams = false;
let loading = false;
let teamsUserId = "";
let syncMessage = "";
let previousMeetings = "";
let previousClickUp = "";
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

function clickupPopup(popup, url) {
  return new Promise((resolve, reject) => {
    const cleanup = () => { window.removeEventListener("message", receive); clearInterval(timer); };
    const receive = (event) => {
      if (event.origin !== location.origin || event.source !== popup || event.data?.type !== "noteiq-clickup") return;
      cleanup();
      event.data.error ? reject(new Error(event.data.error)) : resolve();
    };
    window.addEventListener("message", receive);
    const timer = setInterval(() => {
      if (popup.closed) { cleanup(); reject(new Error("ClickUp connection window closed.")); }
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

function renderMeetings(meetings, clickup) {
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
    const lists = clickup?.lists || [];
    let picker = null;
    if (lists.length > 1) {
      picker = element("select", "", "clickup-list-picker");
      picker.dataset.clickup = "true";
      picker.dataset.hasActions = String(hasActionItems(meeting.content));
      picker.hidden = true;
      for (const item of lists) {
        const option = element("option", item.list_name);
        option.value = item.list_id;
        option.selected = item.is_default;
        picker.append(option);
      }
      buttons.append(picker);
    }
    const clickupButton = element("button", "", "clickup-button");
    clickupButton.dataset.clickup = "true";
    clickupButton.dataset.hasActions = String(hasActionItems(meeting.content));
    clickupButton.hidden = true;
    const clickupIcon = document.createElement("img");
    clickupIcon.src = "/static/clickup.svg";
    clickupIcon.alt = "";
    const clickupLabel = element("span", "Send action items to ClickUp");
    clickupButton.append(clickupIcon, clickupLabel);
    clickupButton.onclick = async () => {
      clickupButton.disabled = true;
      try {
        const body = picker ? {list_id: picker.value} : {};
        const result = await api(`/api/meetings/${meeting.id}/clickup`, body);
        clickupLabel.textContent = result.created ? `${result.created} task${result.created === 1 ? "" : "s"} sent` : "Already sent";
      } catch (error) { showError(error.message); clickupButton.disabled = false; }
    };
    buttons.append(clickupButton);
    article.append(buttons, content, element("p", "Generated from Microsoft 365 Copilot meeting insights.", "hint"));
    $("#meetings").append(article);
    buttons.firstChild.click();
  }
}

function renderClickUp(clickup) {
  $("#clickup-settings").hidden = !clickup.available;
  $("#clickup-shortcut").hidden = !clickup.available;
  if (!clickup.available) return;
  const connected = clickup.connected;
  const lists = clickup.lists || [];
  $("#clickup-connect").hidden = connected;
  $("#clickup-lists-wrap").hidden = !connected;
  $("#clickup-disconnect").hidden = !connected;
  $("#clickup-list-id").value = "";
  $("#clickup-status").textContent = !connected
    ? "Connect your ClickUp account to send action items."
    : !lists.length
    ? "Add a ClickUp List below to start sending action items."
    : clickup.list_id
    ? `New tasks go to "${clickup.list_name}" by default.`
    : "Choose a default ClickUp List below.";
  const listing = $("#clickup-lists");
  listing.replaceChildren();
  for (const item of lists) {
    const row = element("li", "", "clickup-list-row");
    row.append(element("span", item.list_name, "clickup-list-name"));
    if (item.is_default) {
      row.append(element("span", "Default", "clickup-list-badge"));
    } else {
      const makeDefault = element("button", "Set default");
      makeDefault.onclick = async () => {
        makeDefault.disabled = true;
        try { await api("/api/clickup/lists/default", {list_id: item.list_id}); await refresh(); }
        catch (error) { showError(error.message); makeDefault.disabled = false; }
      };
      row.append(makeDefault);
    }
    const remove = element("button", "Remove", "clickup-list-remove");
    remove.onclick = async () => {
      remove.disabled = true;
      try {
        const response = await fetch(`/api/clickup/lists/${item.list_id}`, {
          method: "DELETE",
          headers: {Authorization: `Bearer ${token}`},
        });
        if (!response.ok) throw new Error("Couldn't remove that ClickUp List. Try again.");
        await refresh();
      } catch (error) { showError(error.message); remove.disabled = false; }
    };
    row.append(remove);
    listing.append(row);
  }
  document.querySelectorAll("[data-clickup]").forEach((field) => {
    field.hidden = !lists.length || field.dataset.hasActions !== "true";
  });
}

$("#clickup-shortcut").onclick = () => {
  const settings = $(".account");
  settings.open = true;
  settings.scrollIntoView({behavior: "smooth", block: "start"});
};

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
        : "No saved meeting follow-ups to check yet. Paste a Teams meeting link below to recover one.";
    }
    const [meetings, clickup] = await Promise.all([api("/api/meetings"), api("/api/clickup")]);
    const clickupSignature = JSON.stringify(clickup);
    if (clickupSignature !== previousClickUp) previousMeetings = "";
    previousClickUp = clickupSignature;
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
    renderMeetings(meetings, clickup);
    renderClickUp(clickup);
    showError();
  } catch (error) { showError(error.message); }
  finally { loading = false; $("#refresh").disabled = false; $("#refresh").textContent = "Refresh"; }
}

$("#refresh").onclick = () => refresh(true);
$("#recover-meeting").onsubmit = async (event) => {
  event.preventDefault();
  const button = $("#recover-meeting button");
  button.disabled = true;
  try {
    const result = await api("/api/recover-meeting", {meeting_url: $("#meeting-url").value});
    syncMessage = `Meeting found. Checking ${result.queued ? "transcript and Copilot insights" : "Microsoft 365"}…`;
    setTimeout(() => refresh(), 1500);
  } catch (error) { showError(error.message); }
  finally { button.disabled = false; }
};
$("#clickup-connect").onclick = async () => {
  const popup = inTeams ? null : window.open("about:blank", "noteiq-clickup", "width=600,height=700");
  try {
    if (!inTeams && !popup) throw new Error("Allow popups for NoteIQ, then connect ClickUp again.");
    const {url} = await api("/api/clickup/connect", {in_teams: inTeams});
    if (inTeams) await microsoftTeams.authentication.authenticate({url, width: 600, height: 700});
    else await clickupPopup(popup, url);
    await refresh();
  } catch (error) { if (popup) popup.close(); showError(error.message); }
};
$("#clickup-add-list").onsubmit = async (event) => {
  event.preventDefault();
  const button = event.submitter;
  const original = button.textContent;
  button.disabled = true;
  try {
    await api("/api/clickup/lists", {list_id: $("#clickup-list-id").value});
    $("#clickup-list-id").value = "";
    await refresh();
    button.textContent = "Added";
    setTimeout(() => { button.textContent = original; }, 1500);
  } catch (error) { showError(error.message); }
  finally { button.disabled = false; }
};
$("#clickup-disconnect").onclick = async () => {
  try { await api("/api/clickup/disconnect", {}); await refresh(); }
  catch (error) { showError(error.message); }
};
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
