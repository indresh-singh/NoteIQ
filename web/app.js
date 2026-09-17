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
let previousUploads = "";
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
  previousUploads = "";
  $("#meetings").replaceChildren();
  $("#custom-transcripts").replaceChildren();
  $("#workspace").hidden = true;
  $("#welcome").hidden = false;
}

async function api(path, body, timeoutMs = 20000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
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
  LISTENING: "",
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

function renderNote(note, hideTitle) {
  const line = element("span");
  if (note.title && !hideTitle) line.append(element("strong", note.title + ": "));
  line.append(document.createTextNode(note.text || ""));
  if (!note.subpoints?.length) {
    const paragraph = element("p");
    paragraph.append(line);
    return paragraph;
  }
  const detail = element("details");
  const summary = element("summary");
  summary.append(line);
  detail.append(summary, ...note.subpoints.map((subpoint) => renderNote(subpoint)));
  return detail;
}

function renderCollapsibleNote(note) {
  const wrapper = element("details", "", "note-collapsible");
  const summary = element("summary");
  summary.append(element("strong", note.title || (note.text || "Note").slice(0, 60)));
  wrapper.append(summary, renderNote(note, true));
  return wrapper;
}

function providerLabel(provider) {
  return provider === "openrouter" ? "OpenRouter" : "Microsoft 365 Copilot";
}

function renderMeetings(meetings, clickup, aiProvider, custom = false) {
  const signature = JSON.stringify(meetings) + aiProvider + JSON.stringify(clickup);
  if (signature === (custom ? previousUploads : previousMeetings)) return;
  if (custom) previousUploads = signature;
  else previousMeetings = signature;
  const target = $(custom ? "#custom-transcripts" : "#meetings");
  target.replaceChildren();
  $(custom ? "#uploads-empty" : "#empty").hidden = meetings.length > 0;
  for (const meeting of meetings) {
    const article = element("article", "", "meeting");
    article.append(element("p", custom ? "CUSTOM TRANSCRIPT" : "MEETING FOLLOW-UP", "eyebrow"), element("h2", meeting.subject));

    // Every insight segment carries its own provider tag; group them so the
    // toggle below can show exactly one provider's data at a time instead of
    // merging Copilot's and OpenRouter's notes/action items together.
    const segments = meeting.content.insights || (meeting.content.insight ? [{insight: meeting.content.insight}] : []);
    const providerOf = (segment) => (segment.insight?.provider === "openrouter" ? "openrouter" : "copilot");
    const byProvider = {
      copilot: segments.filter((segment) => providerOf(segment) === "copilot"),
      openrouter: segments.filter((segment) => providerOf(segment) === "openrouter"),
    };
    const hasTranscript = (meeting.content.transcripts || []).length > 0;
    const showToggle = byProvider.copilot.length > 0 || byProvider.openrouter.length > 0 || hasTranscript;
    let selected = byProvider.copilot.length && !byProvider.openrouter.length ? "copilot"
      : byProvider.openrouter.length && !byProvider.copilot.length ? "openrouter"
      : aiProvider === "openrouter" ? "openrouter" : "copilot";

    const dateLine = element("time", "");
    const hintLine = element("p", "", "hint");
    const footerLine = element("p", "", "hint");
    article.append(dateLine, hintLine);

    let activeContentButton = null;

    function updateProviderText() {
      const has = byProvider[selected].length > 0;
      const latest = byProvider[selected].map((segment) => segment.insight).find((insight) => insight?.endDateTime);
      const when = latest?.endDateTime || meeting.content.transcript?.createdDateTime;
      dateLine.textContent = when ? new Date(when).toLocaleString() : "";
      dateLine.hidden = !when;
      hintLine.textContent = has
        ? `${providerLabel(selected)} insights available`
        : hasTranscript
        ? `Transcript ready. Waiting for ${providerLabel(selected)}'s summary and action items.`
        : "Waiting for the transcript.";
      footerLine.textContent = `Generated from ${providerLabel(selected)} meeting insights.`;
    }

    const providerToggle = element("div", "", "provider-toggle");
    const providerStates = element("div", "", "provider-states");
    providerStates.setAttribute("role", "radiogroup");
    providerStates.setAttribute("aria-label", "Meeting insight source");
    const copilotState = element("button", "Copilot", "provider-state");
    const openrouterState = element("button", "OpenRouter", "provider-state");
    for (const state of [copilotState, openrouterState]) {
      state.type = "button";
      state.setAttribute("role", "radio");
    }
    providerStates.append(copilotState, openrouterState);
    const regenerateButton = element("button", "", "regenerate-button");
    regenerateButton.append(element("span", "⟳", "regenerate-icon"), document.createTextNode(" Regenerate"));
    providerToggle.append(providerStates, regenerateButton);
    if (showToggle && !custom) article.append(providerToggle);

    function updateToggle() {
      const openrouterSelected = selected === "openrouter";
      copilotState.setAttribute("aria-checked", String(!openrouterSelected));
      openrouterState.setAttribute("aria-checked", String(openrouterSelected));
      copilotState.tabIndex = openrouterSelected ? -1 : 0;
      openrouterState.tabIndex = openrouterSelected ? 0 : -1;
      regenerateButton.hidden = !openrouterSelected;
    }

    function selectProvider(provider) {
      selected = provider;
      updateProviderText();
      updateToggle();
      if (activeContentButton) activeContentButton.click();
    }
    copilotState.onclick = () => selectProvider("copilot");
    openrouterState.onclick = () => selectProvider("openrouter");
    providerStates.onkeydown = (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const provider = event.key === "ArrowRight" || event.key === "End" ? "openrouter" : "copilot";
      selectProvider(provider);
      (provider === "openrouter" ? openrouterState : copilotState).focus();
    };
    regenerateButton.onclick = async () => {
      regenerateButton.disabled = true;
      regenerateButton.classList.add("spinning");
      try {
        await api(`/api/meetings/${meeting.id}/regenerate`, {}, 90000);
        await refresh();
      } catch (error) {
        showError(error.message);
      } finally {
        regenerateButton.disabled = false;
        regenerateButton.classList.remove("spinning");
      }
    };

    const buttons = element("div", "", "buttons");
    const content = element("div", "", "card-content");
    for (const [label, heading] of [["Summary", "KEY NOTES"], ["Action items", "ACTION ITEMS"]]) {
      const button = element("button", label);
      button.setAttribute("aria-pressed", "false");
      button.onclick = () => {
        activeContentButton = button;
        buttons.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
        content.replaceChildren();
        for (const segment of byProvider[selected]) {
          const insight = segment.insight;
          const items = heading === "KEY NOTES" ? insight?.meetingNotes : insight?.actionItems;
          if (items?.length) {
            content.append(element("h3", heading === "KEY NOTES" ? "Meeting notes" : "Follow-up tasks"));
            for (const item of items) {
              content.append(heading === "KEY NOTES" ? renderCollapsibleNote(item) : renderNote(item));
              if (heading === "ACTION ITEMS") {
                content.append(element("p", item.ownerDisplayName || "Owner not specified", "hint"));
                if (item.dueDate) content.append(element("p", `Due: ${item.dueDate}`, "hint"));
              }
            }
          }
        }
        if (!content.children.length) content.append(element("p", byProvider[selected].length ? `No items were included in the ${providerLabel(selected)} insights.` : `${providerLabel(selected)} insights aren't available yet.`));
      };
      buttons.append(button);
    }
    const transcriptButton = element("button", "Transcripts");
    transcriptButton.setAttribute("aria-pressed", "false");
    transcriptButton.onclick = () => {
      activeContentButton = transcriptButton;
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
    if (custom) {
      article.replaceChildren(element("h2", "Here's your meeting insights"), element("p", meeting.subject, "hint"));
      for (const [label, field] of [["Summary", "meetingNotes"], ["Action Items", "actionItems"]]) {
        const section = element("details", "", "custom-insight-section");
        section.open = true;
        section.append(element("summary", label));
        const items = byProvider.openrouter.flatMap((segment) => segment.insight?.[field] || []);
        if (!items.length) section.append(element("p", "No items identified."));
        for (const item of items) {
          section.append(renderNote(item));
          if (field === "actionItems") {
            section.append(element("p", `Responsible: ${item.ownerDisplayName || "Not specified"}`, "hint"));
            if (item.dueDate) section.append(element("p", `Due: ${item.dueDate}`, "hint"));
          }
        }
        article.append(section);
      }
      const exportActions = element("div", "", "buttons");
      if (picker) exportActions.append(picker);
      exportActions.append(clickupButton);
      article.append(exportActions);
      if (!lists.length) article.append(element("p", "Connect ClickUp and choose a List in Account settings to create tasks.", "hint"));
      target.append(article);
      continue;
    }
    article.append(buttons, content, footerLine);
    updateProviderText();
    updateToggle();
    target.append(article);
    buttons.firstChild.click();
  }
}

let availableClickUpLists = null;
let loadingAvailableClickUpLists = false;

async function ensureAvailableClickUpLists(force = false) {
  if (loadingAvailableClickUpLists || (availableClickUpLists !== null && !force)) return;
  loadingAvailableClickUpLists = true;
  try {
    availableClickUpLists = (await api("/api/clickup/available-lists")).lists;
  } catch (error) {
    availableClickUpLists = [];
    showError(error.message);
  } finally {
    loadingAvailableClickUpLists = false;
    renderClickUpPicker();
  }
}

function renderClickUpPicker() {
  const select = $("#clickup-list-id");
  if (!select) return;
  const addedIds = new Set((currentClickUp?.lists || []).map((item) => item.list_id));
  const options = (availableClickUpLists || []).filter((item) => !addedIds.has(item.id));
  select.replaceChildren();
  select.disabled = true;
  if (availableClickUpLists === null) {
    select.append(new Option("Loading ClickUp Lists…", ""));
  } else if (!options.length) {
    select.append(new Option("No more Lists to add", ""));
  } else {
    select.disabled = false;
    for (const item of options) select.append(new Option(`${item.path} / ${item.name}`, item.id));
  }
  $("#clickup-add-list button[type=submit]").disabled = select.disabled;
}

let currentClickUp = null;

function renderClickUp(clickup) {
  currentClickUp = clickup;
  $("#clickup-settings").hidden = !clickup.available;
  $("#clickup-shortcut").hidden = !clickup.available;
  if (!clickup.available) return;
  const connected = clickup.connected;
  const lists = clickup.lists || [];
  $("#clickup-connect").hidden = connected;
  $("#clickup-lists-wrap").hidden = !connected;
  $("#clickup-disconnect").hidden = !connected;
  if (connected) { ensureAvailableClickUpLists(); renderClickUpPicker(); }
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
        ? "Checking meetings you organized in the last seven days. Results update automatically."
        : "No meeting checks were queued. Please try again.";
    }
    const [meetings, clickup] = await Promise.all([api("/api/meetings"), api("/api/clickup")]);
    const clickupSignature = JSON.stringify(clickup);
    if (clickupSignature !== previousClickUp) previousMeetings = "";
    previousClickUp = clickupSignature;
    if (token !== startedWith) return;
    $("#welcome").hidden = true;
    $("#workspace").hidden = false;
    $("#greeting").textContent = `Welcome, ${user.name}`;
    const statusText = ((statuses[user.status] ?? user.status) + (syncMessage ? " " + syncMessage : "")).trim();
    $("#status").textContent = statusText;
    $(".statusbar").hidden = !statusText;
    const notificationError = user.notifications === "DELIVERY_ERROR";
    $("#notification-status").textContent = notificationError
      ? "Teams notification delivery needs attention. Install or update NoteIQ in Teams and accept its notification permission, then retry."
      : "NoteIQ will notify you in Teams Activity when transcripts and insights are ready.";
    $("#notification-retry").hidden = !notificationError;
    $("#retry").hidden = !["ACCESS_REQUIRED", "CONNECTION_ERROR"].includes(user.status);
    renderMeetings(meetings.filter((m) => m.content.source !== "upload"), clickup, user.ai_provider);
    renderMeetings(meetings.filter((m) => m.content.source === "upload").slice(0, 1), clickup, "openrouter", true);
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
$("#clickup-refresh-lists").onclick = async () => {
  const button = $("#clickup-refresh-lists");
  button.disabled = true;
  await ensureAvailableClickUpLists(true);
  button.disabled = false;
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

function selectWorkspaceTab(upload) {
  for (const [id, selected] of [["upload", upload], ["meetings", !upload]]) {
    const tab = $("#" + id + "-tab");
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    $("#" + id + "-panel").hidden = !selected;
  }
}
$("#upload-tab").onclick = () => selectWorkspaceTab(true);
$("#meetings-tab").onclick = () => selectWorkspaceTab(false);
for (const id of ["upload", "meetings"]) {
  $("#" + id + "-tab").onkeydown = (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const upload = event.key === "End" || (event.key !== "Home" && id === "meetings");
    selectWorkspaceTab(upload);
    $(upload ? "#upload-tab" : "#meetings-tab").focus();
  };
}
$("#upload-transcript").onsubmit = async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  if (button.disabled) return;
  const file = $("#upload-file").files[0];
  if (!file) return;
  button.disabled = true;
  $("#upload-results").hidden = true;
  showError();
  $("#upload-status").textContent = "Reading transcript…";
  try {
    if (!/\.(txt|vtt|srt)$/i.test(file.name) || file.size > 240000) {
      throw new Error("Choose a UTF-8 TXT, VTT or SRT file with up to 60,000 characters.");
    }
    let text;
    try { text = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer()); }
    catch { throw new Error("Save your transcript as UTF-8 text, then upload it again."); }
    if (!text.trim() || [...text].length > 60000) throw new Error("Transcript must contain between 1 and 60,000 characters.");
    $("#upload-status").textContent = "Generating summary and action items…";
    await api("/api/transcripts/upload", {
      subject: $("#upload-title").value.trim(), filename: file.name, text
    }, 90000);
    $("#upload-status").textContent = "Saved. Your summary, action items and transcript appear below.";
    $("#upload-results").hidden = false;
    previousUploads = "";
    await refresh();
  } catch (error) {
    $("#upload-status").textContent = "Analysis did not complete. Your selected file is still available to retry.";
    showError(error.message.replaceAll("OpenRouter", "The AI service"));
  } finally { button.disabled = false; }
};
$("#upload-results").onclick = () => {
  $("#custom-transcripts").scrollIntoView({behavior: "smooth", block: "start"});
  refresh();
};
