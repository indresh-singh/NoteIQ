const $ = (selector) => document.querySelector(selector);
function hasActionItems(content, provider) {
  const insights = content.insights || (content.insight ? [{insight: content.insight}] : []);
  return insights
    .filter((item) => !provider || (item.insight?.provider || "copilot") === provider)
    .some((item) => (item.insight?.actionItems || []).some((a) => a.title || a.text));
}
let token = "";
let inTeams = false;
let loading = false;
let teamsUserId = "";
let syncMessage = "";
let previousMeetings = "";
let previousUploads = "";
let previousClickUp = "";
let chaseId = 0;
const MEETINGS_PAGE_SIZE = 5;
let meetingsPage = 1;
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
  chaseId++;
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
  const requestId = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  try {
  const response = await fetch(path, {
    signal: controller.signal,
    method: body === undefined ? "GET" : "POST",
    headers: {"Content-Type": "application/json", "X-Request-ID": requestId, ...(token ? {Authorization: `Bearer ${token}`} : {})},
    ...(body === undefined ? {} : {body: JSON.stringify(body)})
  });
  if (!response.ok) {
    if (response.status === 401) signedOut();
    let detail;
    try { detail = (await response.json()).detail; } catch {}
    const error = new Error(typeof detail === "string" ? detail : "NoteIQ couldn't complete this request. Please try again.");
    error.requestId = response.headers.get("X-Request-ID") || requestId;
    error.status = response.status;
    throw error;
  }
  return response.json();
  } catch (error) {
    if (error.name === "AbortError") throw new Error("The request timed out. Please try Refresh again.");
    console.error("NoteIQ API request failed", {path, requestId: error.requestId || requestId, status: error.status, error});
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
  if (provider === "openai") return "ChatGPT Enterprise";
  if (provider === "openrouter") return "OpenRouter";
  return "Microsoft 365 Copilot";
}

function renderMeetings(meetings, clickup, planner, summaryProvider, summaryProviders, custom = false) {
  const signature =
    JSON.stringify(meetings) + summaryProvider + JSON.stringify(summaryProviders) + JSON.stringify(clickup) + JSON.stringify(planner);
  if (signature === (custom ? previousUploads : previousMeetings)) return;
  if (custom) previousUploads = signature;
  else previousMeetings = signature;
  const target = $(custom ? "#custom-transcripts" : "#meetings");
  target.replaceChildren();
  $(custom ? "#uploads-empty" : "#empty").hidden = meetings.length > 0;
  // Custom uploads are already capped to one entry by the caller; only the
  // main meetings list is long enough to need paging.
  const totalPages = custom ? 1 : Math.max(1, Math.ceil(meetings.length / MEETINGS_PAGE_SIZE));
  if (!custom) meetingsPage = Math.min(Math.max(1, meetingsPage), totalPages);
  const pageMeetings = custom
    ? meetings
    : meetings.slice((meetingsPage - 1) * MEETINGS_PAGE_SIZE, meetingsPage * MEETINGS_PAGE_SIZE);
  for (const meeting of pageMeetings) {
    const article = element("article", "", "meeting");
    article.append(element("p", custom ? "CUSTOM TRANSCRIPT" : "MEETING FOLLOW-UP", "eyebrow"), element("h2", meeting.subject));

    // Every insight segment carries its own provider tag; group them so the
    // toggle below can show exactly one provider's data at a time.
    const segments = meeting.content.insights || (meeting.content.insight ? [{insight: meeting.content.insight}] : []);
    const providerOrder = ["copilot", "openai", "openrouter"];
    const configuredProviders = new Set(summaryProviders || ["copilot", summaryProvider]);
    const providerOf = (segment) => providerOrder.includes(segment.insight?.provider)
      ? segment.insight.provider : "copilot";
    const byProvider = Object.fromEntries(providerOrder.map((provider) => [
      provider, segments.filter((segment) => providerOf(segment) === provider),
    ]));
    const hasTranscript = (meeting.content.transcripts || []).length > 0;
    const showToggle = providerOrder.some((provider) => byProvider[provider].length) || hasTranscript;
    const providersWithContent = providerOrder.filter((provider) => byProvider[provider].length);
    let selected = providersWithContent.length === 1 ? providersWithContent[0]
      : providersWithContent.includes(summaryProvider) ? summaryProvider
      : providersWithContent[0] || summaryProvider || "copilot";

    const dateLine = element("time", "");
    const hintLine = element("p", "", "hint");
    const syncWarning = element("p", meeting.content.sync_message || "", "meeting-warning");
    syncWarning.hidden = !meeting.content.sync_message;
    syncWarning.setAttribute("role", "status");
    const footerLine = element("p", "", "hint");
    article.append(dateLine, hintLine, syncWarning);

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
    const providerButtons = new Map(providerOrder.map((provider) => [
      provider, element("button", provider === "copilot" ? "Copilot" : providerLabel(provider), "provider-state"),
    ]));
    for (const [provider, state] of providerButtons) {
      state.type = "button";
      state.setAttribute("role", "radio");
      state.onclick = () => selectProvider(provider);
      providerStates.append(state);
    }
    const regenerateButton = element("button", "", "regenerate-button");
    regenerateButton.append(element("span", "⟳", "regenerate-icon"), document.createTextNode(" Regenerate"));
    providerToggle.append(providerStates, regenerateButton);
    if (showToggle && !custom) article.append(providerToggle);

    function updateToggle() {
      const externalSelected = selected !== "copilot";
      for (const [provider, state] of providerButtons) {
        state.setAttribute("aria-checked", String(provider === selected));
        state.tabIndex = provider === selected ? 0 : -1;
      }
      regenerateButton.hidden = !externalSelected;
      regenerateButton.disabled = externalSelected && !configuredProviders.has(selected);
      regenerateButton.title = regenerateButton.disabled
        ? `${providerLabel(selected)} is not configured.` : "";
    }

    function selectProvider(provider) {
      selected = provider;
      updateProviderText();
      updateToggle();
      updateExportVisibility();
      if (activeContentButton) activeContentButton.click();
    }
    providerStates.onkeydown = (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const current = providerOrder.indexOf(selected);
      const index = event.key === "Home" ? 0
        : event.key === "End" ? providerOrder.length - 1
        : (current + (event.key === "ArrowRight" ? 1 : -1) + providerOrder.length) % providerOrder.length;
      const provider = providerOrder[index];
      selectProvider(provider);
      providerButtons.get(provider).focus();
    };
    regenerateButton.onclick = async () => {
      regenerateButton.disabled = true;
      regenerateButton.classList.add("spinning");
      try {
        await api(`/api/meetings/${meeting.id}/regenerate`, {provider: selected}, 90000);
        await refresh();
      } catch (error) {
        showError(error.message);
      } finally {
        regenerateButton.classList.remove("spinning");
        updateToggle();
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
        let headingShown = false;
        for (const segment of byProvider[selected]) {
          const insight = segment.insight;
          const items = heading === "KEY NOTES" ? insight?.meetingNotes : insight?.actionItems;
          if (items?.length) {
            // One heading for the section, not one per stored insight: a meeting
            // transcribed in two parts has two insights and still has one set of notes.
            if (!headingShown) {
              content.append(element("h3", heading === "KEY NOTES" ? "Meeting notes" : "Follow-up tasks"));
              headingShown = true;
            }
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
    clickupButton.hidden = true;
    const clickupIcon = document.createElement("img");
    clickupIcon.src = "/static/clickup.svg";
    clickupIcon.alt = "";
    const clickupDefaultLabel = "Send action items to ClickUp";
    const clickupLabel = element("span", clickupDefaultLabel);
    clickupButton.append(clickupIcon, clickupLabel);
    clickupButton.onclick = async () => {
      clickupButton.disabled = true;
      try {
        const body = {provider: selected, ...(picker ? {list_id: picker.value} : {})};
        const result = await api(`/api/meetings/${meeting.id}/clickup`, body);
        clickupLabel.textContent = result.created ? `${result.created} task${result.created === 1 ? "" : "s"} sent` : "Already sent";
      } catch (error) { showError(error.message); clickupButton.disabled = false; }
    };
    buttons.append(clickupButton);

    const plans = planner?.plans || [];
    let plannerPicker = null;
    if (plans.length > 1) {
      plannerPicker = element("select", "", "clickup-list-picker");
      plannerPicker.dataset.planner = "true";
      plannerPicker.hidden = true;
      for (const item of plans) {
        const option = element("option", item.plan_name);
        option.value = item.plan_id;
        option.selected = item.is_default;
        plannerPicker.append(option);
      }
      buttons.append(plannerPicker);
    }
    const plannerButton = element("button", "Send action items to Planner", "clickup-button");
    plannerButton.dataset.planner = "true";
    plannerButton.hidden = true;
    plannerButton.onclick = async () => {
      plannerButton.disabled = true;
      try {
        const body = {provider: selected, ...(plannerPicker ? {plan_id: plannerPicker.value} : {})};
        const result = await api(`/api/meetings/${meeting.id}/planner`, body);
        plannerButton.textContent = result.created ? `${result.created} task${result.created === 1 ? "" : "s"} sent` : "Already sent";
      } catch (error) { showError(error.message); plannerButton.disabled = false; }
    };
    buttons.append(plannerButton);

    // Export only ever sends whichever provider's items are on screen right
    // now, so the count on the button always matches what was just clicked.
    function updateExportVisibility() {
      const hasActions = hasActionItems(meeting.content, selected);
      const clickupVisible = lists.length > 0 && hasActions;
      clickupButton.dataset.hasActions = String(clickupVisible);
      clickupButton.hidden = !clickupVisible;
      if (picker) {
        picker.dataset.hasActions = String(clickupVisible);
        picker.hidden = !clickupVisible;
      }
      clickupLabel.textContent = clickupDefaultLabel;
      const plannerVisible = plans.length > 0 && hasActions;
      plannerButton.dataset.hasActions = String(plannerVisible);
      plannerButton.hidden = !plannerVisible;
      if (plannerPicker) {
        plannerPicker.dataset.hasActions = String(plannerVisible);
        plannerPicker.hidden = !plannerVisible;
      }
      plannerButton.textContent = "Send action items to Planner";
    }
    updateExportVisibility();
    if (custom) {
      article.replaceChildren(element("h2", "Here's your meeting insights"), element("p", meeting.subject, "hint"));
      for (const [label, field] of [["Summary", "meetingNotes"], ["Action Items", "actionItems"]]) {
        const section = element("details", "", "custom-insight-section");
        section.open = true;
        section.append(element("summary", label));
        const items = byProvider[selected].flatMap((segment) => segment.insight?.[field] || []);
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
      if (plannerPicker) exportActions.append(plannerPicker);
      exportActions.append(plannerButton);
      article.append(exportActions);
      if (!lists.length && !plans.length) article.append(element("p", "Connect ClickUp or add a Microsoft Planner plan in Account settings to create tasks.", "hint"));
      target.append(article);
      continue;
    }
    article.append(buttons, content, footerLine);
    updateProviderText();
    updateToggle();
    target.append(article);
    buttons.firstChild.click();
  }
  if (!custom && meetings.length > MEETINGS_PAGE_SIZE) {
    const pager = element("nav", "", "pagination");
    pager.setAttribute("aria-label", "Meetings pages");
    const goToPage = (page) => {
      meetingsPage = page;
      previousMeetings = ""; // force a rebuild even though the data hasn't changed
      renderMeetings(meetings, clickup, planner, summaryProvider, summaryProviders);
    };
    const previous = element("button", "Previous");
    previous.type = "button";
    previous.disabled = meetingsPage <= 1;
    previous.onclick = () => goToPage(meetingsPage - 1);
    const status = element("span", `Page ${meetingsPage} of ${totalPages}`, "pagination-status");
    status.setAttribute("aria-live", "polite");
    const next = element("button", "Next");
    next.type = "button";
    next.disabled = meetingsPage >= totalPages;
    next.onclick = () => goToPage(meetingsPage + 1);
    pager.append(previous, status, next);
    target.append(pager);
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

let availablePlannerPlans = null;
let availablePlannerPlansFailed = false;
let loadingAvailablePlannerPlans = false;

async function ensureAvailablePlannerPlans(force = false) {
  if (loadingAvailablePlannerPlans || (availablePlannerPlans !== null && !force)) return;
  loadingAvailablePlannerPlans = true;
  try {
    availablePlannerPlans = (await api("/api/planner/available-plans")).plans;
    availablePlannerPlansFailed = false;
  } catch (error) {
    availablePlannerPlans = [];
    availablePlannerPlansFailed = true;
    // Planner has no on/off switch to gate this on, so this call now fires
    // for every signed-in user, not just ones who opted in. A tenant that
    // hasn't granted the Planner Graph permissions yet would otherwise show
    // every user an error banner just for opening Account settings; only an
    // explicit "Refresh" click (force=true) surfaces one.
    if (force) showError(error.message);
  } finally {
    loadingAvailablePlannerPlans = false;
    renderPlannerPicker();
  }
}

function renderPlannerPicker() {
  const select = $("#planner-plan-id");
  if (!select) return;
  const addedIds = new Set((currentPlanner?.plans || []).map((item) => item.plan_id));
  const options = (availablePlannerPlans || []).filter((item) => !addedIds.has(item.id));
  select.replaceChildren();
  select.disabled = true;
  if (availablePlannerPlans === null) {
    select.append(new Option("Loading Planner plans…", ""));
  } else if (availablePlannerPlansFailed) {
    select.append(new Option("Couldn't load plans. Click Refresh to try again.", ""));
  } else if (!options.length) {
    select.append(new Option("No more plans to add", ""));
  } else {
    select.disabled = false;
    for (const item of options) select.append(new Option(`${item.path} / ${item.name}`, item.id));
  }
  $("#planner-add-plan button[type=submit]").disabled = select.disabled;
}

async function refreshPlannerTasks() {
  const listing = $("#planner-tasks");
  const empty = $("#planner-tasks-empty");
  if (!currentPlanner?.plan_id) return;
  try {
    const {tasks} = await api(`/api/planner/tasks?plan_id=${encodeURIComponent(currentPlanner.plan_id)}`);
    listing.replaceChildren();
    empty.hidden = tasks.length > 0;
    for (const task of tasks) {
      const row = element("li", "", "clickup-list-row");
      row.append(element("span", task.title || "Untitled task", "clickup-list-name"));
      const detail = [task.bucket_name, task.percent_complete ? `${task.percent_complete}% complete` : "Not started", task.due_date ? `Due ${new Date(task.due_date).toLocaleDateString()}` : ""].filter(Boolean).join(" · ");
      if (detail) row.append(element("span", detail, "hint"));
      listing.append(row);
    }
  } catch (error) { showError(error.message); }
}

let currentPlanner = null;

function renderPlanner(planner) {
  currentPlanner = planner;
  $("#planner-settings").hidden = false;
  const plans = planner.plans || [];
  $("#planner-plans-wrap").hidden = false;
  ensureAvailablePlannerPlans();
  renderPlannerPicker();
  $("#planner-status").textContent = !plans.length
    ? "Add a Planner plan below to start sending action items."
    : planner.plan_id
    ? `New tasks go to "${planner.plan_name}" by default.`
    : "Choose a default Planner plan below.";
  const listing = $("#planner-plans");
  listing.replaceChildren();
  for (const item of plans) {
    const row = element("li", "", "clickup-list-row");
    row.append(element("span", item.plan_name, "clickup-list-name"));
    if (item.is_default) {
      row.append(element("span", "Default", "clickup-list-badge"));
    } else {
      const makeDefault = element("button", "Set default");
      makeDefault.onclick = async () => {
        makeDefault.disabled = true;
        try { await api("/api/planner/plans/default", {plan_id: item.plan_id}); await refresh(); }
        catch (error) { showError(error.message); makeDefault.disabled = false; }
      };
      row.append(makeDefault);
    }
    const remove = element("button", "Remove", "clickup-list-remove");
    remove.onclick = async () => {
      remove.disabled = true;
      try {
        const response = await fetch(`/api/planner/plans/${item.plan_id}`, {
          method: "DELETE",
          headers: {Authorization: `Bearer ${token}`},
        });
        if (!response.ok) throw new Error("Couldn't remove that Planner plan. Try again.");
        await refresh();
      } catch (error) { showError(error.message); remove.disabled = false; }
    };
    row.append(remove);
    listing.append(row);
  }
  $("#planner-tasks-wrap").hidden = !planner.plan_id;
  document.querySelectorAll("[data-planner]").forEach((field) => {
    field.hidden = !plans.length || field.dataset.hasActions !== "true";
  });
}

$("#clickup-shortcut").onclick = () => {
  const settings = $(".account");
  settings.open = true;
  settings.scrollIntoView({behavior: "smooth", block: "start"});
};

// A sync only queues the fetch; the worker completes it a moment later. Re-read
// a few times on a tightening schedule so the card fills in without waiting for
// the 15-second interval. Passes run one after another, so refresh()'s `loading`
// guard never silently drops one.
async function chaseResults() {
  const mine = ++chaseId;
  for (const delay of [2000, 3000, 5000, 10000]) {
    await new Promise((resolve) => setTimeout(resolve, delay));
    if (mine !== chaseId || !token || document.hidden) return;
    await refresh();
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
        ? "Found new activity. Fetching the details now — results update automatically."
        : "Checked Microsoft 365 just now. You're all caught up.";
      if (result.queued) chaseResults();
    }
    const [meetings, clickup, planner] = await Promise.all([
      api("/api/meetings"), api("/api/clickup"), api("/api/planner"),
    ]);
    const clickupSignature = JSON.stringify(clickup) + JSON.stringify(planner);
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
      : "NoteIQ will notify you once in Teams Activity when a meeting's AI insights are ready.";
    $("#notification-retry").hidden = !notificationError;
    $("#retry").hidden = !["ACCESS_REQUIRED", "CONNECTION_ERROR"].includes(user.status);
    renderMeetings(meetings.filter((m) => m.content.source !== "upload"), clickup, planner, user.summary_provider, user.summary_providers);
    renderMeetings(meetings.filter((m) => m.content.source === "upload").slice(0, 1), clickup, planner, user.summary_provider, user.summary_providers, true);
    renderClickUp(clickup);
    renderPlanner(planner);
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
    syncMessage = result.message || `Meeting found. Checking ${result.queued ? "transcript and Copilot insights" : "Microsoft 365"}…`;
    setTimeout(() => refresh(), 1500);
  } catch (error) {
    if ([403, 409].includes(error.status)) {
      // Ownership/access responses are durable meeting outcomes, not a
      // transient toast. Keep the explanation in the status bar so the next
      // 15-second refresh does not erase it before the user can read it.
      syncMessage = error.message;
      $("#status").textContent = syncMessage;
      $(".statusbar").hidden = false;
      showError();
    } else showError(error.message);
  }
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
$("#planner-add-plan").onsubmit = async (event) => {
  event.preventDefault();
  const button = event.submitter;
  const original = button.textContent;
  button.disabled = true;
  try {
    await api("/api/planner/plans", {plan_id: $("#planner-plan-id").value});
    $("#planner-plan-id").value = "";
    await refresh();
    button.textContent = "Added";
    setTimeout(() => { button.textContent = original; }, 1500);
  } catch (error) { showError(error.message); }
  finally { button.disabled = false; }
};
$("#planner-refresh-plans").onclick = async () => {
  const button = $("#planner-refresh-plans");
  button.disabled = true;
  await ensureAvailablePlannerPlans(true);
  button.disabled = false;
};
$("#planner-refresh-tasks").onclick = async () => {
  const button = $("#planner-refresh-tasks");
  button.disabled = true;
  await refreshPlannerTasks();
  button.disabled = false;
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
const MAX_TRANSCRIPT_CHARS = 60000;
const MAX_TEXT_FILE_BYTES = 240000;
const MAX_DOCX_FILE_BYTES = 10 * 1024 * 1024;

async function extractTranscriptText(file) {
  const isDocx = /\.docx$/i.test(file.name);
  if (isDocx) {
    if (file.size > MAX_DOCX_FILE_BYTES) {
      throw new Error("Choose a DOCX file smaller than 10 MB.");
    }
    if (!window.mammoth?.extractRawText) {
      throw new Error("DOCX reading is unavailable. Reload NoteIQ and try again.");
    }
    let result;
    try {
      result = await window.mammoth.extractRawText({arrayBuffer: await file.arrayBuffer()});
    } catch {
      throw new Error("NoteIQ couldn't read that DOCX file. It may be malformed or password-protected.");
    }
    if (!result.value.trim()) {
      throw new Error("The DOCX contains no readable text. It may be empty or image-only.");
    }
    return result.value;
  }
  if (!/\.(txt|vtt|srt)$/i.test(file.name) || file.size > MAX_TEXT_FILE_BYTES) {
    throw new Error("Choose a Teams DOCX or UTF-8 TXT, VTT or SRT transcript.");
  }
  try {
    return new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer());
  } catch {
    throw new Error("Save your transcript as UTF-8 text, then upload it again.");
  }
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
    const text = await extractTranscriptText(file);
    if (!text.trim() || [...text].length > MAX_TRANSCRIPT_CHARS) {
      throw new Error("Transcript must contain between 1 and 60,000 characters.");
    }
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
