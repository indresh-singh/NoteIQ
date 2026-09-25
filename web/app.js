const $ = (selector) => document.querySelector(selector);
function hasActionItems(content, provider) {
  const insights = content.insights || (content.insight ? [{insight: content.insight}] : []);
  return insights
    .filter((item) => !provider || (item.insight?.provider || "copilot") === provider)
    .some((item) => (item.insight?.actionItems || []).some((a) => a.title || a.text));
}
let token = "";
let lastSessionRenewal = -Infinity;
let renewingSession = false;
let inTeams = false;
let loading = false;
let regenerating = 0;
let teamsUserId = "";
let syncMessage = "";
let previousMeetings = "";
let previousUploads = "";
let previousClickUp = "";
let chaseId = 0;
let waitId = 0;
// Whether syncMessage describes a Graph throttle pause; cleared with the pause.
let syncThrottled = false;
let pauseTimer = 0;
const THROTTLE_BANNER = "Microsoft 365 is limiting requests. New meetings and insights are delayed; NoteIQ will catch up automatically.";
// Refresh checks Graph inline for up to 80 seconds (REFRESH_DEADLINE_SECONDS),
// so the browser must outwait it or a finished check would read as a timeout.
const SYNC_TIMEOUT_MS = 90000;
const MEETINGS_PAGE_SIZE = 5;
let meetingsPage = 1;
try { token = sessionStorage.getItem("noteiq-session") || ""; } catch { /* Memory-only fallback. */ }

function remember(value) {
  token = value;
  lastSessionRenewal = -Infinity;
  try { value ? sessionStorage.setItem("noteiq-session", value) : sessionStorage.removeItem("noteiq-session"); } catch {}
}

function showError(message = "") {
  $("#error").textContent = message;
  $("#error").hidden = !message;
}

function showRecoveryStatus(message = "") {
  $("#recovery-status").textContent = message;
  $("#recovery-status").hidden = !message;
}

function signedOut() {
  showRecoveryStatus();
  syncMessage = "";
  syncThrottled = false;
  clearTimeout(pauseTimer);
  chaseId++;
  remember("");
  previousMeetings = "";
  previousUploads = "";
  occurrenceOpen.clear();
  occurrenceProvider.clear();
  $("#meetings").replaceChildren();
  $("#custom-transcripts").replaceChildren();
  $("#clickup-shortcut").hidden = true;
  $("#planner-shortcut").hidden = true;
  $("#workspace").hidden = true;
  $("#welcome").hidden = false;
}

async function api(path, body, timeoutMs = 30000) {
  const requestToken = token;
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
    if (response.status === 401 && token === requestToken) signedOut();
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

async function connect(planner = false) {
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
    const {url} = await api(planner ? "/api/planner/connect" : "/api/auth/start", {challenge, in_teams: inTeams});
    const code = inTeams
      ? await microsoftTeams.authentication.authenticate({url, width:600, height:650})
      : await popupResult(popup, url);
    const result = await api("/api/auth/complete", {code, verifier});
    remember(result.token);
    invalidatePlannerPlans();
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
  MISSED_EVENTS: "Some updates were missed while NoteIQ was offline. Your administrator can recover a meeting using its link.",
  UPDATES_DELAYED: "Microsoft 365 is limiting requests, so new meeting updates are delayed. NoteIQ will reconnect and catch up automatically."
};

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function createDestinationSelect(items, valueField, labelField, className, accessibleName) {
  const select = element("select", "", `destination-select ${className}`);
  select.setAttribute("aria-label", accessibleName);
  for (const item of items) {
    const option = element("option", item[labelField]);
    option.value = item[valueField];
    option.selected = Boolean(item.is_default);
    select.append(option);
  }
  return select;
}

// Only real interaction renews the eight-hour idle window. Background polling
// must not keep an unattended tab signed in indefinitely.
async function renewActiveSession(event) {
  if (!event.isTrusted || document.hidden || !token || renewingSession) return;
  const now = performance.now();
  if (now - lastSessionRenewal < 5 * 60 * 1000) return;
  lastSessionRenewal = now;
  renewingSession = true;
  try { await api("/api/session/renew", {}); }
  catch { /* api handles expiry; transient errors retry on later activity. */ }
  finally { renewingSession = false; }
}
for (const activity of ["pointerdown", "keydown", "wheel", "touchstart"]) {
  document.addEventListener(activity, renewActiveSession, {passive: true});
}

function optionalText(value) {
  if (typeof value !== "string" || value.trim().toLowerCase() === "null") return "";
  return value;
}

function renderNote(note, hideTitle) {
  const line = element("span");
  const title = optionalText(note.title);
  const text = optionalText(note.text);
  if (title && !hideTitle) line.append(element("strong", title + ": "));
  line.append(document.createTextNode(text));
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
  summary.append(element("strong", optionalText(note.title) || optionalText(note.text) || "Note"));
  wrapper.append(summary, renderNote(note, true));
  return wrapper;
}

function providerLabel(provider) {
  if (provider === "openai") return "ChatGPT Enterprise";
  if (provider === "openrouter") return "OpenRouter";
  return "Microsoft 365 Copilot";
}

function occurrenceLabel(subject, startedAt) {
  const date = new Date(startedAt);
  if (!startedAt || Number.isNaN(date.getTime())) return `${subject} - Date unavailable`;
  const day = `${date.getDate()} ${date.toLocaleDateString("en-US", {month: "short"})}`;
  const hour = date.getHours() % 12 || 12;
  const minute = String(date.getMinutes()).padStart(2, "0");
  return `${subject} - ${day} ${hour}.${minute} ${date.getHours() < 12 ? "AM" : "PM"}`;
}

function renderActions(items, numbered) {
  const list = element(numbered ? "ol" : "div", "", "insight-actions");
  for (const item of items) {
    const row = element(numbered ? "li" : "div");
    row.append(renderNote(item), element("p", `Owner: ${optionalText(item.ownerDisplayName) || "Not specified"}`, "hint"));
    if (optionalText(item.dueDate)) row.append(element("p", `Due: ${item.dueDate}`, "hint"));
    list.append(row);
  }
  return list;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c]);
}

function emailDate(value, withTime) {
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(value || "");
  const date = new Date(dateOnly ? `${value}T00:00:00` : value);
  if (!value || Number.isNaN(date.getTime())) return value || "";
  const options = {weekday: "long", day: "numeric", month: "long", year: "numeric"};
  if (withTime && !dateOnly) Object.assign(options, {hour: "numeric", minute: "2-digit", hour12: true});
  return date.toLocaleString("en-GB", options);
}

// Builds the draft as email-safe HTML (tables and inline styles only, so it
// survives Outlook's Word renderer) plus a plain-text fallback for mailto.
function meetingEmail(meeting, segments) {
  const title = meeting.subject || "Meeting";
  const when = emailDate(meeting.content.started_at || meeting.content.meeting_metadata?.start_date_time, true);
  const provider = segments[0]?.insight?.provider;
  const participants = (meeting.content.meeting_metadata?.participants || []).filter((person) => person.name || person.email);
  const participantNames = participants.map((person) => person.name || person.email).join(", ");
  // The organizer sends the minutes, so only attendees go on the To line.
  const to = [...new Set(participants.filter((person) => !person.organizer && person.email).map((person) => person.email))];
  const numbered = provider === "openai";
  const sections = [["Meeting Summary", "meetingNotes"], ["Action Items", "actionItems"]].map(([heading, field]) => ({
    heading, field, items: segments.flatMap((segment) => segment.insight?.[field] || []),
  }));
  const font = "font-family:'Segoe UI',-apple-system,BlinkMacSystemFont,Helvetica,Arial,sans-serif";

  const noteHtml = (note) => {
    const noteTitle = optionalText(note.title);
    const text = optionalText(note.text);
    const lead = noteTitle ? `<strong style="color:#242638">${escapeHtml(noteTitle)}${text ? ":" : ""}</strong> ` : "";
    return lead + escapeHtml(text) + subpointsHtml(note.subpoints);
  };
  const subpointsHtml = (points) => points?.length
    ? `<ul style="margin:6px 0 0;padding-left:20px;color:#4a4e63">${points
        .map((point) => `<li style="margin:0 0 4px">${noteHtml(point)}</li>`).join("")}</ul>`
    : "";
  const pill = (label, value) =>
    `<span style="display:inline-block;margin:8px 8px 0 0;padding:3px 10px;border-radius:12px;background:#ffffff;border:1px solid #dfe2f2;font-size:12px;color:#64697c">` +
    `<span style="font-weight:600;color:#5056b8;letter-spacing:.4px">${label}</span>&nbsp; ${escapeHtml(value)}</span>`;
  const actionHtml = (item, index) => {
    const marker = numbered
      ? `<span style="display:inline-block;min-width:22px;height:22px;line-height:22px;margin-right:8px;border-radius:11px;background:#5056b8;color:#ffffff;font-size:12px;font-weight:700;text-align:center">${index + 1}</span>`
      : `<span style="color:#5056b8;font-weight:700;margin-right:6px">&#9744;</span>`;
    const due = optionalText(item.dueDate);
    return `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:separate;margin:0 0 10px">` +
      `<tr><td style="padding:12px 16px;background:#f7f8fc;border:1px solid #e4e7f0;border-left:4px solid #5056b8;border-radius:8px;${font};font-size:14px;line-height:1.55;color:#242638">` +
      `${marker}${noteHtml(item)}<div>${pill("OWNER", optionalText(item.ownerDisplayName) || "Not specified")}` +
      `${due ? pill("DUE", emailDate(due)) : ""}</div></td></tr></table>`;
  };
  const sectionHtml = ({heading, field, items}) => {
    const count = items.length ? ` <span style="font-size:12px;font-weight:600;color:#64697c">(${items.length})</span>` : "";
    const body = !items.length
      ? `<p style="margin:0;color:#64697c;font-style:italic">No items identified.</p>`
      : field === "actionItems"
        ? items.map(actionHtml).join("")
        : `<ul style="margin:0;padding-left:20px">${items.map((item) => `<li style="margin:0 0 10px">${noteHtml(item)}</li>`).join("")}</ul>`;
    return `<h2 style="margin:26px 0 12px;padding-bottom:8px;border-bottom:2px solid #eceefa;${font};font-size:17px;font-weight:700;color:#5056b8">${heading}${count}</h2>${body}`;
  };
  const html =
    `<div style="${font};font-size:14px;line-height:1.6;color:#242638">` +
    `<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;border-collapse:separate">` +
    `<tr><td style="background:#5056b8;padding:22px 28px;border-radius:12px 12px 0 0;${font}">` +
    `<div style="font-size:11px;font-weight:600;letter-spacing:1.6px;color:#d6d8ff">MINUTES OF MEETING</div>` +
    `<div style="margin-top:4px;font-size:22px;font-weight:700;line-height:1.3;color:#ffffff">${escapeHtml(title)}</div>` +
    (when ? `<div style="margin-top:6px;font-size:13px;color:#e4e5ff">${escapeHtml(when)}</div>` : "") +
    `</td></tr><tr><td style="background:#ffffff;border:1px solid #e4e7f0;border-top:0;border-radius:0 0 12px 12px;padding:4px 28px 26px;${font};font-size:14px;line-height:1.6;color:#242638">` +
    (participantNames
      ? `<p style="margin:22px 0 0;padding:10px 14px;background:#f7f8fc;border-radius:8px;font-size:13px;color:#4a4e63">` +
        `<span style="font-weight:600;color:#5056b8;letter-spacing:.4px">PARTICIPANTS</span>&nbsp; ${escapeHtml(participantNames)}</p>`
      : "") +
    sections.map(sectionHtml).join("") +
    `<p style="margin:26px 0 0;padding-top:12px;border-top:1px solid #eceefa;font-size:12px;color:#8a8fa3"><a href="${escapeHtml(window.location.origin)}/" style="color:#0563c1;text-decoration:underline">Sent from NoteIQ</a></p>` +
    `</td></tr></table></div>`;

  const noteLines = (note, depth = 0) => {
    const text = [optionalText(note.title), optionalText(note.text)].filter(Boolean).join(": ");
    return [
      ...(text ? [`${"    ".repeat(depth)}${depth ? "-" : "•"} ${text}`] : []),
      ...(note.subpoints || []).flatMap((point) => noteLines(point, depth + 1)),
    ];
  };
  const lines = [`MINUTES OF MEETING - ${title.toUpperCase()}`, ...(when ? [when] : []),
    ...(participantNames ? [`Participants: ${participantNames}`] : [])];
  for (const {heading, field, items} of sections) {
    lines.push("", heading.toUpperCase(), "─".repeat(heading.length), "");
    if (!items.length) lines.push("No items identified.");
    for (const [index, item] of items.entries()) {
      const formatted = noteLines(item);
      if (field === "actionItems" && numbered && formatted.length) formatted[0] = formatted[0].replace(/^• /, `${index + 1}. `);
      lines.push(...formatted);
      if (field === "actionItems") {
        const due = optionalText(item.dueDate);
        lines.push(`    Owner: ${optionalText(item.ownerDisplayName) || "Not specified"}${due ? `  |  Due: ${emailDate(due)}` : ""}`, "");
      }
    }
  }
  while (lines.at(-1) === "") lines.pop();
  lines.push("", "Sent from NoteIQ");
  const subject = `Minutes of Meeting - ${title}`.replace(/[\r\n]+/g, " ");
  return {subject, to, html, text: lines.join("\r\n")};
}

// Copies the draft as rich text so pasting keeps the formatting; mailto
// bodies can only carry plain text.
async function copyRichText(html, text) {
  try {
    if (inTeams && microsoftTeams.clipboard?.isSupported()) {
      await microsoftTeams.clipboard.write(new Blob([html], {type: "text/html"}));
      return true;
    }
  } catch { /* Fall through to the browser clipboard. */ }
  try {
    if (navigator.clipboard?.write && window.ClipboardItem) {
      await navigator.clipboard.write([new ClipboardItem({
        "text/html": new Blob([html], {type: "text/html"}),
        "text/plain": new Blob([text], {type: "text/plain"}),
      })]);
      return true;
    }
  } catch { /* Clipboard API blocked (e.g. iframe policy); try the copy event. */ }
  let copied = false;
  const onCopy = (event) => {
    event.clipboardData.setData("text/html", html);
    event.clipboardData.setData("text/plain", text);
    event.preventDefault();
    copied = true;
  };
  document.addEventListener("copy", onCopy);
  try { document.execCommand("copy"); } catch {} finally { document.removeEventListener("copy", onCopy); }
  return copied;
}

const occurrenceOpen = new Map();
const occurrenceProvider = new Map();

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
  for (const umbrella of pageMeetings) {
    const occurrences = umbrella.content.occurrences;
    const grouped = !custom && occurrences && (
      umbrella.content.meeting_metadata?.meeting_type === "recurring" || occurrences.length > 1
    );
    let parent = target;
    if (grouped) {
      parent = element("article", "", "meeting meeting-series");
      parent.append(element("p", umbrella.content.meeting_metadata?.meeting_type === "recurring"
        ? "RECURRING MEETING" : "MEETING CALLS", "eyebrow"), element("h2", umbrella.subject));
      parent.append(element("p", `${occurrences.length} sessions · Latest first`, "hint"));
      target.append(parent);
    }
    if (occurrences && umbrella.content.unassigned_insights) {
      parent.append(element("p", "Some older summaries cover the whole meeting or series and cannot be matched to the entries below. Select an entry and use Regenerate for ChatGPT Enterprise or OpenRouter. Copilot results appear when Microsoft provides matching transcript details.", "hint"));
    }
    if (occurrences && !occurrences.length) {
      parent.append(element("p", "Waiting for transcripts to identify this meeting's sessions.", "hint"));
    }
    const views = occurrences ? occurrences.map((session) => ({
      ...umbrella, occurrence_id: session.id,
      content: {...umbrella.content, transcripts: session.transcripts, insights: session.insights,
        transcript: session.transcripts[0]?.transcript, insight: null, started_at: session.started_at,
        metadata_pending: session.metadata_pending},
    })) : [umbrella];
    for (const [sessionIndex, meeting] of views.entries()) {
    const viewKey = `${meeting.id}:${meeting.occurrence_id || "single"}`;
    let destination = parent;
    if (grouped) {
      const details = element("details", "", "meeting-occurrence");
      details.open = occurrenceOpen.get(viewKey) ?? sessionIndex === 0;
      details.ontoggle = () => occurrenceOpen.set(viewKey, details.open);
      details.append(element("summary", occurrenceLabel(meeting.subject, meeting.content.started_at)));
      parent.append(details);
      destination = details;
    }
    const article = element("article", "", "meeting");
    if (!grouped) article.append(element("p", custom ? "CUSTOM TRANSCRIPT" : "MEETING FOLLOW-UP", "eyebrow"), element("h2", meeting.subject));
    if (!grouped && !custom && meeting.content.transcripts?.length > 1) {
      article.append(element("p", `${meeting.content.transcripts.length} transcript parts combined for this meeting`, "hint"));
    }
    if (meeting.content.metadata_pending) article.append(element("p", "Session details unavailable; this transcript is kept separately.", "hint"));

    // Every insight segment carries its own provider tag; group them so the
    // toggle below can show exactly one provider's data at a time.
    const segments = meeting.content.insights || (meeting.content.insight ? [{insight: meeting.content.insight}] : []);
    const providerOrder = ["openai", "copilot", "openrouter"];
    const configuredProviders = new Set(summaryProviders || ["copilot", summaryProvider]);
    const providerOf = (segment) => providerOrder.includes(segment.insight?.provider)
      ? segment.insight.provider : "copilot";
    const byProvider = Object.fromEntries(providerOrder.map((provider) => [
      provider, segments.filter((segment) => providerOf(segment) === provider),
    ]));
    const hasTranscript = (meeting.content.transcripts || []).length > 0;
    const showToggle = providerOrder.some((provider) => byProvider[provider].length) || hasTranscript;
    const providersWithContent = providerOrder.filter((provider) => byProvider[provider].length);
    const openaiReady = configuredProviders.has("openai");
    // ChatGPT opens first whenever it is configured, even before its summary
    // exists, so the Regenerate prompt is the first thing the organizer sees.
    let selected = !custom && (openaiReady || byProvider.openai.length) ? "openai"
      : providersWithContent.length === 1 ? providersWithContent[0]
      : providersWithContent.includes(summaryProvider) ? summaryProvider
      : providersWithContent[0] || summaryProvider || "copilot";
    selected = occurrenceProvider.get(viewKey) || selected;

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
      const metadata = meeting.content.meeting_metadata || {};
      const transcript = meeting.content.transcript || {};
      const when = meeting.content.started_at || metadata.start_date_time
        || metadata.end_date_time || transcript.endDateTime || transcript.createdDateTime;
      dateLine.textContent = when ? new Date(when).toLocaleString() : "";
      dateLine.hidden = !when;
      const promptRegenerate = !has && hasTranscript && selected === "openai" && openaiReady && !custom;
      hintLine.textContent = has
        ? `${providerLabel(selected)} insights available`
        : promptRegenerate
        ? "Transcript ready. Summary and Action Items are not available yet. Click Regenerate to create them."
        : hasTranscript
        ? `Transcript ready. Waiting for ${providerLabel(selected)}'s summary and action items.`
        : "Waiting for the transcript.";
      hintLine.classList.toggle("regenerate-prompt", promptRegenerate);
      regenerateButton.classList.toggle("attention", promptRegenerate);
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
      occurrenceProvider.set(viewKey, provider);
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
      let counted = false;
      try {
        // Let an already-running refresh finish before generation starts. Once
        // counted, periodic refreshes stand aside so they cannot replace this
        // card while its response is being applied to the live DOM.
        while (loading && token) await new Promise((resolve) => setTimeout(resolve, 50));
        if (!token) return;
        regenerating += 1;
        counted = true;
        const requestedProvider = selected;
        const result = await api(`/api/meetings/${meeting.id}/regenerate`, {
          provider: requestedProvider, occurrence_id: meeting.occurrence_id,
        }, 90000);
        byProvider[result.provider] = result.insights || [];
        if (selected === result.provider) {
          updateProviderText();
          updateExportVisibility();
          if (activeContentButton) activeContentButton.click();
        }
        showError();
      } catch (error) {
        showError(error.message);
      } finally {
        if (counted) regenerating -= 1;
        regenerateButton.classList.remove("spinning");
        updateToggle();
      }
    };

    const buttons = element("div", "", "buttons tab-buttons");
    const actionButtons = element("div", "", "buttons action-buttons");
    const content = element("div", "", "card-content");
    for (const [label, heading] of [["Summary", "KEY NOTES"], ["Action items", "ACTION ITEMS"]]) {
      const button = element("button", label);
      button.setAttribute("aria-pressed", "false");
      button.onclick = () => {
        activeContentButton = button;
        buttons.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
        content.replaceChildren();
        if (heading === "ACTION ITEMS") {
          const items = byProvider[selected].flatMap((segment) => segment.insight?.actionItems || []);
          if (items.length) {
            content.append(element("h3", "Follow-up tasks"), renderActions(items, selected === "openai"));
          } else {
            content.append(element("p", byProvider[selected].length ? `No action items were included in the ${providerLabel(selected)} insights.` : `${providerLabel(selected)} insights aren't available yet.`));
          }
          return;
        }
        let headingShown = false;
        for (const segment of byProvider[selected]) {
          const insight = segment.insight;
          const items = insight?.meetingNotes;
          if (items?.length) {
            // One heading for the section, not one per stored insight: a meeting
            // transcribed in two parts has two insights and still has one set of notes.
            if (!headingShown) {
              content.append(element("h3", "Meeting notes"));
              headingShown = true;
            }
            for (const item of items) {
              content.append(renderCollapsibleNote(item));
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
      const transcripts = [...(meeting.content.transcripts || [])].sort((a, b) =>
        (Date.parse(b.transcript.createdDateTime) || 0) - (Date.parse(a.transcript.createdDateTime) || 0));
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
    const clickupActions = element("div", "", "export-destination");
    clickupActions.dataset.clickup = "true";
    clickupActions.hidden = true;
    const clickupSelect = createDestinationSelect(
      lists, "list_id", "list_name", "clickup-picker", "ClickUp List",
    );
    const clickupButton = element("button", "", "clickup-button send-button");
    const clickupIcon = document.createElement("img");
    clickupIcon.src = "/static/clickup.svg";
    clickupIcon.alt = "";
    const clickupDefaultLabel = "Send action items to ClickUp";
    const clickupLabel = element("span", clickupDefaultLabel);
    clickupButton.append(clickupIcon, clickupLabel);
    clickupButton.onclick = async () => {
      clickupButton.disabled = true;
      try {
        const body = {provider: selected, list_id: clickupSelect.value, occurrence_id: meeting.occurrence_id};
        const result = await api(`/api/meetings/${meeting.id}/clickup`, body);
        clickupLabel.textContent = result.created ? `${result.created} task${result.created === 1 ? "" : "s"} sent` : "Already sent";
      } catch (error) { showError(error.message); clickupButton.disabled = false; }
    };
    clickupActions.append(clickupSelect, clickupButton);
    actionButtons.append(clickupActions);

    const plans = planner?.plans || [];
    const plannerActions = element("div", "", "export-destination");
    plannerActions.dataset.planner = "true";
    plannerActions.hidden = true;
    const plannerSelect = createDestinationSelect(
      plans, "plan_id", "plan_name", "planner-picker", "Microsoft Planner plan",
    );
    const plannerButton = element("button", "", "clickup-button send-button");
    const plannerIcon = document.createElement("img");
    plannerIcon.src = "/static/planner.svg";
    plannerIcon.alt = "";
    const plannerDefaultLabel = "Send action items to Planner";
    const plannerLabel = element("span", plannerDefaultLabel);
    plannerButton.append(plannerIcon, plannerLabel);
    plannerButton.onclick = async () => {
      plannerButton.disabled = true;
      try {
        const body = {provider: selected, plan_id: plannerSelect.value, occurrence_id: meeting.occurrence_id};
        const result = await api(`/api/meetings/${meeting.id}/planner`, body);
        plannerLabel.textContent = result.created ? `${result.created} task${result.created === 1 ? "" : "s"} sent` : "Already sent";
      } catch (error) { showError(error.message); plannerButton.disabled = false; }
    };
    plannerActions.append(plannerSelect, plannerButton);
    actionButtons.append(plannerActions);

    const emailButton = element("button", "Emails", "send-button");
    emailButton.type = "button";
    emailButton.title = "Open an email draft with the meeting summary and action items";
    emailButton.onclick = async () => {
      const email = meetingEmail(meeting, byProvider[selected]);
      const copied = await copyRichText(email.html, email.text);
      const body = copied ? "" : `&body=${encodeURIComponent(email.text)}`;
      const to = email.to.map((address) => encodeURIComponent(address).replace(/%40/g, "@")).join(",");
      window.location.href = `mailto:${to}?subject=${encodeURIComponent(email.subject)}${body}`;
      if (!copied) return;
      emailButton.textContent = "Copied - paste into your email";
      emailButton.title = "The formatted summary is on your clipboard. Paste it into the email body (Ctrl+V or Cmd+V).";
      setTimeout(() => {
        emailButton.textContent = "Emails";
        emailButton.title = "Open an email draft with the meeting summary and action items";
      }, 6000);
    };
    actionButtons.append(emailButton);

    // Export only ever sends whichever provider's items are on screen right
    // now, so the count on the button always matches what was just clicked.
    function updateExportVisibility() {
      emailButton.disabled = !byProvider[selected].length;
      const hasActions = hasActionItems(meeting.content, selected);
      const clickupVisible = lists.length > 0 && hasActions;
      clickupActions.dataset.hasActions = String(clickupVisible);
      clickupActions.hidden = !clickupVisible;
      clickupLabel.textContent = clickupDefaultLabel;
      clickupButton.disabled = false;
      const plannerVisible = plans.length > 0 && hasActions;
      plannerActions.dataset.hasActions = String(plannerVisible);
      plannerActions.hidden = !plannerVisible;
      plannerLabel.textContent = plannerDefaultLabel;
      plannerButton.disabled = false;
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
        if (field === "actionItems") section.append(renderActions(items, selected === "openai"));
        else for (const item of items) section.append(renderNote(item));
        article.append(section);
      }
      const exportActions = element("div", "", "buttons action-buttons");
      exportActions.append(clickupActions, plannerActions, emailButton);
      article.append(exportActions);
      if (!lists.length && !plans.length) article.append(element("p", "Connect ClickUp or add a Microsoft Planner plan in Account settings to create tasks.", "hint"));
      destination.append(article);
      continue;
    }
    article.append(buttons);
    article.append(actionButtons);
    article.append(content, footerLine);
    updateProviderText();
    updateToggle();
    destination.append(article);
    buttons.firstChild.click();
    }
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
  $("#clickup-connect-label").textContent = connected ? "Reconnect ClickUp" : "Connect ClickUp";
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
let plannerDiscoveryGeneration = 0;

function invalidatePlannerPlans() {
  plannerDiscoveryGeneration += 1;
  availablePlannerPlans = null;
  availablePlannerPlansFailed = false;
}

async function ensureAvailablePlannerPlans(force = false) {
  if (loadingAvailablePlannerPlans || (availablePlannerPlans !== null && !force)) return;
  loadingAvailablePlannerPlans = true;
  const generation = plannerDiscoveryGeneration;
  const startedWith = token;
  try {
    const result = await api("/api/planner/available-plans");
    if (token !== startedWith || generation !== plannerDiscoveryGeneration) return;
    availablePlannerPlans = result.plans;
    availablePlannerPlansFailed = false;
  } catch (error) {
    if (token !== startedWith || generation !== plannerDiscoveryGeneration) return;
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
  const empty = $("#planner-plans-empty");
  empty.hidden = availablePlannerPlans === null || availablePlannerPlansFailed
    || availablePlannerPlans.length > 0 || addedIds.size > 0;
  select.replaceChildren();
  select.disabled = true;
  if (availablePlannerPlans === null) {
    select.append(new Option("Loading Planner plans…", ""));
  } else if (availablePlannerPlansFailed) {
    select.append(new Option("Couldn't load plans. Click Refresh to try again.", ""));
  } else if (!options.length) {
    select.append(new Option(addedIds.size
      ? "No additional Planner plans found" : "No Planner plans found", ""));
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
  if (currentPlanner?.delegated_connected !== planner.delegated_connected) invalidatePlannerPlans();
  currentPlanner = planner;
  $("#planner-connect-label").textContent = planner.delegated_connected ? "Reconnect Microsoft Planner" : "Connect Microsoft Planner";
  $("#planner-disconnect").hidden = !planner.delegated_connected;
  $("#planner-settings").hidden = false;
  $("#planner-shortcut").hidden = false;
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

function openIntegrationSettings(target) {
  const settings = $(".account");
  settings.open = true;
  target.scrollIntoView({behavior: "smooth", block: "start"});
}

$("#clickup-shortcut").onclick = () => openIntegrationSettings($("#clickup-settings"));
$("#planner-shortcut").onclick = () => openIntegrationSettings($("#planner-settings"));

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

const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;

// "Found new activity" means a meeting or an AI insight that is not on screen
// yet. A new transcript for a meeting already shown is fetched, not announced.
function syncOutcome(result) {
  const found = [];
  if (result.new_meetings) found.push(plural(result.new_meetings, "new meeting"));
  if (result.new_insights) found.push(plural(result.new_insights, "new AI insight"));
  if (result.throttled) {
    // The background work is paused too, so nothing is promised for "later".
    const limited = "Microsoft 365 is limiting requests right now."
      + (result.retry_after ? ` Try again in about ${plural(result.retry_after, "second")}.` : "");
    if (found.length) return `Found new activity: ${found.join(" and ")}. The details will load once Microsoft 365 allows it. ${limited}`;
    if (!result.checked) return limited;
    return `No new activity in ${result.checked} of ${plural(result.total, "meeting")} checked. ${limited}`;
  }
  if (!result.complete) {
    const rest = `${result.checked} of ${plural(result.total, "meeting")} checked; the rest will be checked in the background.`;
    return found.length
      ? `Found new activity: ${found.join(" and ")}. Fetching the details now — results update automatically. ${rest}`
      : `No new activity so far: ${rest}`;
  }
  if (found.length) return `Found new activity: ${found.join(" and ")}. Fetching the details now — results update automatically.`;
  return "Checked Microsoft 365 just now. You're all caught up.";
}

function showSyncOutcome(result) {
  syncMessage = syncOutcome(result);
  syncThrottled = Boolean(result.throttled);
}

const foundAnything = (result) => Boolean(result.new_meetings || result.new_insights);

// Another tab (or an earlier click) is already checking. Wait for that check to
// finish and show its outcome, instead of starting a second one.
async function waitForRefresh() {
  const mine = ++waitId;
  const giveUp = Date.now() + 130000;  // just past the server's two-minute lock
  while (Date.now() < giveUp) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    if (mine !== waitId || !token) return;
    let status;
    try { status = await api("/api/sync/status"); } catch { continue; }
    if (status.running) continue;
    if (status.result) showSyncOutcome(status.result);
    // refresh() skips while another pass (the 15-second tick, a focus event) is
    // drawing; wait that one out so this outcome shows now, not on the next tick.
    while (loading) await new Promise((resolve) => setTimeout(resolve, 250));
    await refresh();
    if (status.result && foundAnything(status.result)) chaseResults();
    return;
  }
}

async function refresh(sync = false) {
  if (!token || loading || regenerating) return;
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
    const limited = user.graph_throttled_seconds > 0;
    // A throttled Refresh message describes a pause that has now ended.
    if (syncThrottled && !limited) { syncMessage = ""; syncThrottled = false; }
    if (sync) {
      const result = await api("/api/sync", {}, SYNC_TIMEOUT_MS);
      if (result.status === "already_running") {
        syncMessage = "Already checking Microsoft 365. Results appear here when that check finishes.";
        waitForRefresh();
      } else {
        showSyncOutcome(result);
        if (foundAnything(result)) chaseResults();
      }
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
    // UPDATES_DELAYED and a throttled Refresh message already say this.
    const banner = limited && user.status !== "UPDATES_DELAYED" && !syncThrottled ? THROTTLE_BANNER : "";
    const statusText = [statuses[user.status] ?? user.status, banner, syncMessage].filter(Boolean).join(" ").trim();
    // Re-read as the pause ends so the banner clears then, not up to 15s later.
    clearTimeout(pauseTimer);
    if (limited) pauseTimer = setTimeout(() => refresh(), (user.graph_throttled_seconds + 1) * 1000);
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
  const requestToken = token;
  button.disabled = true;
  showError();
  showRecoveryStatus("Looking up the meeting in Microsoft 365…");
  try {
    const result = await api("/api/recover-meeting", {meeting_url: $("#meeting-url").value}, 60000);
    if (token !== requestToken) return;
    // This acknowledges a lookup/queue operation, not newly fetched insights.
    // Keep it beside the form so polling, throttling and Refresh cannot erase it.
    showRecoveryStatus(result.message || (result.queued
      ? "Meeting found. A background check for transcripts and Copilot insights was queued. Any newly available results will appear automatically; existing results may stay unchanged."
      : "Meeting found. No additional check was queued. Existing results may stay unchanged; use Refresh to check Microsoft 365."));
    chaseResults();
  } catch (error) {
    if (token === requestToken) showRecoveryStatus(error.message);
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
$("#planner-connect").onclick = async () => {
  const button = $("#planner-connect");
  button.disabled = true;
  try { await connect(true); } finally { button.disabled = false; }
};
$("#planner-disconnect").onclick = async () => {
  try {
    await api("/api/planner/disconnect", {});
    invalidatePlannerPlans();
    await refresh();
  } catch (error) { showError(error.message); }
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
