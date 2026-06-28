let state = null;

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail || error.error || "Request failed");
  }
  return response.json();
}

async function loadState() {
  state = await api("/api/state");
  render();
}

function render() {
  $("automation").textContent = state.settings.automation_enabled ? "Running" : "Paused";
  $("accountCount").textContent = `${state.accounts.filter((a) => a.enabled).length}/${state.accounts.length}`;
  $("groupCount").textContent = `${state.groups.filter((g) => g.enabled).length}/${state.groups.length}`;
  $("cycle").textContent = `${state.settings.cycle_hours}h`;
  $("toggle").textContent = state.settings.automation_enabled ? "Pause" : "Start";
  $("toggle").className = state.settings.automation_enabled ? "danger" : "primary";

  $("cycleHours").value = state.settings.cycle_hours;
  $("delay").value = state.settings.per_account_delay_seconds;
  $("action").value = state.settings.action;
  $("keywords").value = state.settings.keywords.join(", ");
  $("responseMessage").value = state.settings.response_message;

  $("accountsList").innerHTML = state.accounts.map(accountRow).join("");
  $("groupsList").innerHTML = state.groups.map(groupRow).join("");
  $("detections").innerHTML = state.detections.map(detectionItem).join("") || empty("No detections yet.");
  $("systemLogs").innerHTML = state.logs.map(logItem).join("") || empty("No logs yet.");
}

function accountRow(account) {
  return `
    <div class="row">
      <div><strong>${escapeHtml(account.label)}</strong><span>${escapeHtml(account.session_string)}</span>${account.last_error ? `<span>${escapeHtml(account.last_error)}</span>` : ""}</div>
      <span class="status ${account.status}">${account.status}</span>
      <button class="ghost" onclick="toggleAccount('${account.id}', ${!account.enabled})">${account.enabled ? "Disable" : "Enable"}</button>
    </div>
  `;
}

function groupRow(group) {
  return `
    <div class="row">
      <div><strong>${escapeHtml(group.title)}</strong><span>${escapeHtml(group.identifier)}</span></div>
      <span class="status ${group.enabled ? "online" : ""}">${group.enabled ? "active" : "paused"}</span>
      <button class="ghost" onclick="toggleGroup('${group.id}', ${!group.enabled})">${group.enabled ? "Disable" : "Enable"}</button>
    </div>
  `;
}

function detectionItem(item) {
  const account = state.accounts.find((entry) => entry.id === item.account_id);
  const group = state.groups.find((entry) => entry.id === item.group_id);
  return `
    <div class="item">
      <strong>${escapeHtml(item.matched_keyword)} · ${escapeHtml(item.action_status)}</strong>
      <p>${escapeHtml(item.message_preview)}</p>
      <span>${escapeHtml(account?.label || "Unknown account")} · ${escapeHtml(group?.title || "Unknown group")} · ${formatDate(item.detected_at)}</span>
    </div>
  `;
}

function logItem(item) {
  return `
    <div class="item">
      <strong>${escapeHtml(item.level)} · ${escapeHtml(item.message)}</strong>
      <span>${formatDate(item.created_at)}</span>
    </div>
  `;
}

function empty(message) {
  return `<div class="item"><span>${message}</span></div>`;
}

async function saveRules() {
  await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({
      cycle_hours: Number($("cycleHours").value),
      per_account_delay_seconds: Number($("delay").value),
      action: $("action").value,
      keywords: $("keywords").value.split(",").map((item) => item.trim()),
      response_message: $("responseMessage").value,
    }),
  });
  await loadState();
}

async function toggleAutomation() {
  await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({ automation_enabled: !state.settings.automation_enabled }),
  });
  await loadState();
}

async function toggleAccount(id, enabled) {
  await api(`/api/accounts/${id}`, { method: "PATCH", body: JSON.stringify({ enabled }) });
  await loadState();
}

async function toggleGroup(id, enabled) {
  await api(`/api/groups/${id}`, { method: "PATCH", body: JSON.stringify({ enabled }) });
  await loadState();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[char]);
}

function formatDate(value) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

$("toggle").addEventListener("click", toggleAutomation);
$("saveRules").addEventListener("click", saveRules);
$("demo").addEventListener("click", async () => {
  await api("/api/demo/detection", { method: "POST" });
  await loadState();
});

$("accountForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  await api("/api/accounts", {
    method: "POST",
    body: JSON.stringify({
      label: $("accountLabel").value,
      session_string: $("sessionString").value,
    }),
  });
  event.target.reset();
  await loadState();
});

$("groupForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  await api("/api/groups", {
    method: "POST",
    body: JSON.stringify({
      title: $("groupTitle").value,
      identifier: $("groupIdentifier").value,
    }),
  });
  event.target.reset();
  await loadState();
});

loadState();
setInterval(loadState, 7000);
