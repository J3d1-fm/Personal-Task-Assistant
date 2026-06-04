const statuses = ["backlog", "in_progress", "waiting_review", "blocked", "done"];
const statusLabels = {
  backlog: "Backlog",
  in_progress: "In progress",
  waiting_review: "Review",
  blocked: "Blocked",
  done: "Done",
};
const filterLabels = {
  all: "All",
  codex_ready: "Agent queue",
  human_input: "Human input",
  mine: "My tasks",
  review: "Needs review",
  overdue: "Overdue",
  unassigned: "Unassigned",
};
const legacyFilters = {
  codex: "codex_ready",
};

const form = document.querySelector("#taskForm");
const searchInput = document.querySelector("#taskSearch");
const sortSelect = document.querySelector("#taskSort");
const filterButtons = [...document.querySelectorAll("[data-filter]")];
let dragState = null;
let allTasks = [];
let activeFilter = normalizeFilter(localStorage.getItem("taskTrackerFilter"));
let activeSort = localStorage.getItem("taskTrackerSort") || "smart";
let activeSearch = "";

function headers() {
  const apiKey = localStorage.getItem("taskTrackerApiKey");
  const values = { "Content-Type": "application/json" };
  if (apiKey) values.Authorization = `Bearer ${apiKey}`;
  return values;
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...headers(),
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

function localDateTimeToIso(value) {
  if (!value) return null;
  return new Date(value).toISOString();
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(form);
  const payload = Object.fromEntries(data.entries());
  payload.priority = Number(payload.priority);
  payload.origin = "manual";
  payload.due_at = localDateTimeToIso(payload.due_at);
  payload.reminder_at = localDateTimeToIso(payload.reminder_at);
  if (!payload.description) payload.description = null;
  try {
    await request("/api/tasks", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    form.reset();
    form.querySelector("[name=priority]").value = "3";
    activeFilter = "all";
    persistFilter();
    await loadTasks();
  } catch (error) {
    alert(`Could not create task: ${error.message}`);
  }
});

filterButtons.forEach((button) => {
  button.addEventListener("click", () => {
    activeFilter = button.dataset.filter;
    persistFilter();
    renderTasks();
  });
});

searchInput.addEventListener("input", () => {
  activeSearch = searchInput.value.trim().toLowerCase();
  renderTasks();
});

sortSelect.addEventListener("change", () => {
  activeSort = sortSelect.value;
  persistSort();
  renderTasks();
});

function replaceTask(id, nextTask) {
  allTasks = allTasks.map((task) => (task.id === id ? nextTask : task));
}

async function updateTask(id, patch) {
  const currentTask = allTasks.find((task) => task.id === id);
  if (!currentTask) return;

  const optimisticTask = {
    ...currentTask,
    ...patch,
    updated_at: new Date().toISOString(),
    completed_at: patch.status === "done" ? new Date().toISOString() : patch.status ? null : currentTask.completed_at,
  };
  replaceTask(id, optimisticTask);
  renderTasks();

  try {
    const savedTask = await request(`/api/tasks/${id}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    replaceTask(id, savedTask);
    renderTasks();
  } catch (error) {
    replaceTask(id, currentTask);
    renderTasks();
    throw error;
  }
}

async function deleteTask(id) {
  const previousTasks = allTasks;
  allTasks = allTasks.filter((task) => task.id !== id);
  renderTasks();

  try {
    await request(`/api/tasks/${id}`, {
      method: "DELETE",
    });
  } catch (error) {
    allTasks = previousTasks;
    renderTasks();
    throw error;
  }
}

function persistFilter() {
  activeFilter = normalizeFilter(activeFilter);
  localStorage.setItem("taskTrackerFilter", activeFilter);
  filterButtons.forEach((button) => {
    const isActive = button.dataset.filter === activeFilter;
    button.classList.toggle("is-active", isActive);
    button.setAttribute("aria-pressed", String(isActive));
  });
}

function normalizeFilter(value) {
  const migrated = legacyFilters[value] || value || "all";
  return filterLabels[migrated] ? migrated : "all";
}

function persistSort() {
  localStorage.setItem("taskTrackerSort", activeSort);
  sortSelect.value = activeSort;
}

function isActiveTask(task) {
  return !["done", "cancelled"].includes(task.status);
}

function activeTasks(tasks = allTasks) {
  return tasks.filter(isActiveTask);
}

function isOverdue(task) {
  if (!isActiveTask(task)) return false;
  const targets = [task.due_at, task.reminder_at].filter(Boolean).map((value) => new Date(value));
  return targets.some((value) => !Number.isNaN(value.valueOf()) && value < new Date());
}

function isDueSoon(task) {
  if (!isActiveTask(task)) return false;
  const targets = [task.due_at, task.reminder_at].filter(Boolean).map((value) => new Date(value));
  const now = new Date();
  const soon = new Date(now.getTime() + 24 * 60 * 60 * 1000);
  return targets.some((value) => !Number.isNaN(value.valueOf()) && value >= now && value <= soon);
}

function isCodexReady(task) {
  return isActiveTask(task) && task.assignee === "codex" && ["backlog", "in_progress"].includes(task.status);
}

function needsHumanInput(task) {
  return isActiveTask(task) && (task.assignee === "me" || task.status === "waiting_review" || task.status === "blocked");
}

function isNeutralTask(task) {
  if (task.priority <= 2) return false;
  const text = `${task.title} ${task.description || ""}`.toLowerCase();
  return /\b(thanks?|thank you|reply thanks|simple feedback)\b|спасибо|поблагодар|простой фидбек|ответить спасибо/.test(text);
}

function priorityClass(task) {
  const classes = ["task"];
  if (!isNeutralTask(task)) {
    if (task.priority <= 1) classes.push("priority-urgent");
    else if (task.priority === 2) classes.push("priority-important");
    else classes.push("priority-normal");
  }
  if (isOverdue(task)) classes.push("is-overdue");
  if (isDueSoon(task)) classes.push("is-due-soon");
  return classes.join(" ");
}

function priorityLabel(task) {
  if (isOverdue(task)) return `P${task.priority} overdue`;
  if (task.priority === 1) return "P1 urgent";
  if (task.priority === 2) return "P2 important";
  if (task.priority === 4) return "P4 low";
  if (task.priority === 5) return "P5 someday";
  return "P3 normal";
}

function priorityTooltip(task) {
  if (isOverdue(task)) return "Overdue: the due date or reminder time has already passed.";
  if (isDueSoon(task)) return "Due soon: the due date or reminder is within the next 24 hours.";
  if (task.priority === 1) return "P1 urgent: default DD is about 1 day if no source deadline is provided.";
  if (task.priority === 2) return "P2 important: default DD is about 3 days if no source deadline is provided.";
  if (task.priority === 4) return "P4 low: default DD is about 14 days if no source deadline is provided.";
  if (task.priority === 5) return "P5 someday: default DD is about 30 days if no source deadline is provided.";
  return "P3 normal: default DD is about 7 days if no source deadline is provided.";
}

function assigneeLabel(value) {
  if (value === "me") return "Me";
  if (value === "codex") return "Codex";
  return "Unassigned";
}

function assigneeTooltip(value) {
  if (value === "me") return "You own the next action.";
  if (value === "codex") return "Codex owns the next action.";
  return "No owner yet. Triage this before work starts.";
}

function statusTooltip(value) {
  if (value === "backlog") return "New, unstarted, or waiting for triage.";
  if (value === "in_progress") return "Work that is actively moving.";
  if (value === "waiting_review") return "Output is ready and needs review before closing.";
  if (value === "blocked") return "Cannot move forward without an answer, access, decision, or file.";
  if (value === "done") return "Completed and accepted.";
  return "Task status.";
}

function formatDateTime(value) {
  return new Intl.DateTimeFormat(undefined, {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function titleCaseStatus(value) {
  return statusLabels[value] || value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function buttonClass(isActive) {
  return isActive ? "is-active" : "secondary";
}

function tooltipAttr(value) {
  return `data-tooltip="${escapeHtml(value)}" title="${escapeHtml(value)}"`;
}

function clearDropTargets() {
  document.querySelectorAll(".column.is-drop-target").forEach((column) => column.classList.remove("is-drop-target"));
}

function measureColumns() {
  return [...document.querySelectorAll(".column")].map((column) => ({
    node: column,
    rect: column.getBoundingClientRect(),
  }));
}

function columnAtPoint(x, y) {
  return dragState?.columns.find(({ rect }) => x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom)?.node || null;
}

function setDropTarget(column) {
  if (dragState?.dropTarget === column) return;
  dragState?.dropTarget?.classList.remove("is-drop-target");
  column?.classList.add("is-drop-target");
  if (dragState) dragState.dropTarget = column;
}

function resetDragNode(node) {
  node.classList.remove("is-dragging");
  node.style.transform = "";
  node.style.zIndex = "";
  node.style.willChange = "";
}

function setupTaskDrag(node, task) {
  node.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest("button, input, select, textarea, a")) return;
    dragState = {
      taskId: task.id,
      pointerId: event.pointerId,
      node,
      startX: event.clientX,
      startY: event.clientY,
      active: false,
      columns: [],
      dropTarget: null,
    };
    node.setPointerCapture(event.pointerId);
  });

  node.addEventListener("pointermove", (event) => {
    if (!dragState || dragState.pointerId !== event.pointerId || dragState.node !== node) return;
    const deltaX = event.clientX - dragState.startX;
    const deltaY = event.clientY - dragState.startY;
    if (!dragState.active && Math.hypot(deltaX, deltaY) < 8) return;
    dragState.active = true;
    if (dragState.columns.length === 0) dragState.columns = measureColumns();
    node.classList.add("is-dragging");
    node.style.zIndex = "10";
    node.style.willChange = "transform";
    node.style.transform = `translate(${deltaX}px, ${deltaY}px)`;
    setDropTarget(columnAtPoint(event.clientX, event.clientY));
  });

  node.addEventListener("pointerup", async (event) => {
    if (!dragState || dragState.pointerId !== event.pointerId || dragState.node !== node) return;
    const { active, taskId } = dragState;
    const column = active ? columnAtPoint(event.clientX, event.clientY) : null;
    dragState = null;
    clearDropTargets();
    resetDragNode(node);
    node.releasePointerCapture(event.pointerId);
    const status = column?.dataset.status;
    if (!active || !status || status === task.status) return;
    try {
      await updateTask(taskId, { status });
    } catch (error) {
      alert(`Could not move task: ${error.message}`);
    }
  });

  node.addEventListener("pointercancel", (event) => {
    if (!dragState || dragState.pointerId !== event.pointerId || dragState.node !== node) return;
    dragState = null;
    clearDropTargets();
    resetDragNode(node);
  });
}

function datePills(task) {
  const pills = [];
  if (task.created_at) {
    pills.push(`<span class="pill date-pill" ${tooltipAttr(`Date set: ${new Date(task.created_at).toLocaleString()}`)}>Set ${escapeHtml(formatDateTime(task.created_at))}</span>`);
  }
  if (task.due_at) {
    pills.push(`<span class="pill date-pill" ${tooltipAttr(`DD: ${new Date(task.due_at).toLocaleString()}. If the source did not provide a date, the server estimates DD from priority.`)}>DD ${escapeHtml(formatDateTime(task.due_at))}</span>`);
  }
  if (task.reminder_at) {
    pills.push(`<span class="pill date-pill" ${tooltipAttr(`Reminder time: ${new Date(task.reminder_at).toLocaleString()}`)}>Remind ${escapeHtml(formatDateTime(task.reminder_at))}</span>`);
  }
  return pills.join("");
}

function sourceMarkup(task) {
  const name = task.source_name ? escapeHtml(task.source_name) : "";
  if (task.source_url) {
    return `<a class="source-link" href="${escapeHtml(task.source_url)}" target="_blank" rel="noreferrer" ${tooltipAttr("Open the original source for this task.")}>${name || "Source"}</a>`;
  }
  return name ? `<span class="pill" ${tooltipAttr("Where this task came from.")}>${name}</span>` : "";
}

function taskNode(task) {
  const node = document.createElement("article");
  node.className = priorityClass(task);
  node.dataset.taskId = task.id;
  node.setAttribute("aria-label", `${task.title}, ${titleCaseStatus(task.status)}`);
  node.innerHTML = `
    <div class="task-topline">
      <span class="status-dot" aria-hidden="true"></span>
      <span class="status-name" ${tooltipAttr(statusTooltip(task.status))}>${escapeHtml(titleCaseStatus(task.status))}</span>
      <span class="task-id">#${task.id}</span>
    </div>
    <h3>${escapeHtml(task.title)}</h3>
    ${task.description ? `<p>${escapeHtml(task.description)}</p>` : ""}
    <div class="meta">
      <span class="pill priority-pill" ${tooltipAttr(priorityTooltip(task))}>${escapeHtml(priorityLabel(task))}</span>
      <span class="pill assignee-${escapeHtml(task.assignee)}" ${tooltipAttr(assigneeTooltip(task.assignee))}>${escapeHtml(assigneeLabel(task.assignee))}</span>
      <span class="pill" ${tooltipAttr("Source channel that created the task.")}>${escapeHtml(task.origin)}</span>
      ${sourceMarkup(task)}
      ${datePills(task)}
    </div>
    <div class="actions">
      <button class="${buttonClass(task.assignee === "me")}" data-action="me" aria-label="Assign to me" aria-pressed="${task.assignee === "me"}" ${tooltipAttr("Make yourself the next-action owner.")}>Me</button>
      <button class="${buttonClass(task.assignee === "codex")}" data-action="codex" aria-label="Assign to Codex" aria-pressed="${task.assignee === "codex"}" ${tooltipAttr("Delegate the next action to Codex.")}>Codex</button>
      <button class="${buttonClass(task.status === "in_progress")}" data-action="start" aria-pressed="${task.status === "in_progress"}" ${tooltipAttr("Move this task to In progress.")}>Start</button>
      <button class="${buttonClass(task.status === "waiting_review")}" data-action="review" aria-pressed="${task.status === "waiting_review"}" ${tooltipAttr("Move this task to Review.")}>Review</button>
      <button class="${buttonClass(task.status === "done")}" data-action="done" aria-pressed="${task.status === "done"}" ${tooltipAttr("Close this task as accepted.")}>Done</button>
      <button class="secondary danger-action" data-action="dismiss" ${tooltipAttr("Permanently delete this task from the database.")}>Dismiss</button>
    </div>
  `;
  setupTaskDrag(node, task);
  node.querySelector('[data-action="me"]').addEventListener("click", () => applyTaskPatch(task.id, { assignee: "me" }, "assign task"));
  node.querySelector('[data-action="codex"]').addEventListener("click", () => applyTaskPatch(task.id, { assignee: "codex" }, "assign task"));
  node.querySelector('[data-action="start"]').addEventListener("click", () => applyTaskPatch(task.id, { status: "in_progress" }, "move task"));
  node.querySelector('[data-action="review"]').addEventListener("click", () => applyTaskPatch(task.id, { status: "waiting_review" }, "move task"));
  node.querySelector('[data-action="done"]').addEventListener("click", () => applyTaskPatch(task.id, { status: "done" }, "close task"));
  node.querySelector('[data-action="dismiss"]').addEventListener("click", async () => {
    if (!confirm(`Dismiss task #${task.id}? This deletes it from the database.`)) return;
    try {
      await deleteTask(task.id);
    } catch (error) {
      alert(`Could not dismiss task: ${error.message}`);
    }
  });
  return node;
}

async function applyTaskPatch(id, patch, actionLabel) {
  try {
    await updateTask(id, patch);
  } catch (error) {
    alert(`Could not ${actionLabel}: ${error.message}`);
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function matchesFilter(task) {
  if (activeFilter === "codex_ready") return isCodexReady(task);
  if (activeFilter === "human_input") return needsHumanInput(task);
  if (activeFilter === "mine") return task.assignee === "me" && isActiveTask(task);
  if (activeFilter === "review") return task.status === "waiting_review";
  if (activeFilter === "overdue") return isOverdue(task);
  if (activeFilter === "unassigned") return task.assignee === "unassigned" && isActiveTask(task);
  return true;
}

function matchesSearch(task) {
  if (!activeSearch) return true;
  const haystack = [
    task.title,
    task.description,
    task.source_name,
    task.source_context,
    task.origin,
    task.assignee,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return haystack.includes(activeSearch);
}

function taskRank(task) {
  let rank = task.priority * 100;
  if (isOverdue(task)) rank -= 70;
  if (isDueSoon(task)) rank -= 30;
  if (isCodexReady(task)) rank -= 18;
  if (task.status === "waiting_review") rank -= 12;
  if (task.status === "blocked") rank += 15;
  if (!isActiveTask(task)) rank += 200;
  return rank;
}

function dateValue(value, fallback) {
  if (!value) return fallback;
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? fallback : parsed.getTime();
}

function dueSortValue(task) {
  return Math.min(
    dateValue(task.due_at, Number.POSITIVE_INFINITY),
    dateValue(task.reminder_at, Number.POSITIVE_INFINITY),
  );
}

function sortTasks(tasks) {
  return [...tasks].sort((a, b) => {
    if (activeSort === "due") {
      return dueSortValue(a) - dueSortValue(b) || a.priority - b.priority || dateValue(b.updated_at, 0) - dateValue(a.updated_at, 0);
    }
    if (activeSort === "created") {
      return dateValue(b.created_at, 0) - dateValue(a.created_at, 0) || a.priority - b.priority;
    }
    if (activeSort === "updated") {
      return dateValue(b.updated_at, 0) - dateValue(a.updated_at, 0) || a.priority - b.priority;
    }
    if (activeSort === "owner") {
      return a.assignee.localeCompare(b.assignee) || dueSortValue(a) - dueSortValue(b) || a.priority - b.priority;
    }
    const rankDelta = taskRank(a) - taskRank(b);
    if (rankDelta !== 0) return rankDelta;
    return dueSortValue(a) - dueSortValue(b) || dateValue(b.updated_at, 0) - dateValue(a.updated_at, 0);
  });
}

function updateMetrics() {
  const active = activeTasks();
  const metrics = {
    active: active.length,
    overdue: allTasks.filter(isOverdue).length,
    codex_ready: active.filter(isCodexReady).length,
    human_input: active.filter(needsHumanInput).length,
    review: allTasks.filter((task) => task.status === "waiting_review").length,
  };
  Object.entries(metrics).forEach(([key, value]) => {
    const node = document.querySelector(`[data-metric="${key}"]`);
    if (node) node.textContent = value;
  });
}

function emptyMessage(status) {
  const activeFilterLabel = filterLabels[activeFilter] || filterLabels.all;
  if (activeFilter !== "all" || activeSearch) return `No ${activeFilterLabel.toLowerCase()} here`;
  if (status === "backlog") return "Nothing to triage";
  if (status === "in_progress") return "No active work";
  if (status === "waiting_review") return "Nothing waiting for review";
  if (status === "blocked") return "Nothing blocked";
  return "No completed tasks";
}

function renderTasks() {
  persistFilter();
  updateMetrics();
  const filteredTasks = sortTasks(allTasks.filter((task) => matchesFilter(task) && matchesSearch(task)));

  for (const status of statuses) {
    const bucket = document.querySelector(`#${status}`);
    const items = filteredTasks.filter((task) => task.status === status);
    document.querySelector(`[data-count="${status}"]`).textContent = items.length;
    bucket.innerHTML = "";
    if (items.length === 0) {
      bucket.innerHTML = `<div class="empty">${escapeHtml(emptyMessage(status))}</div>`;
    } else {
      for (const task of items) bucket.appendChild(taskNode(task));
    }
  }
}

async function loadTasks() {
  for (const status of statuses) {
    document.querySelector(`#${status}`).innerHTML = '<div class="empty">Loading tasks</div>';
  }
  try {
    allTasks = await request("/api/tasks?include_done=true");
    renderTasks();
  } catch (error) {
    for (const status of statuses) {
      document.querySelector(`#${status}`).innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
    }
  }
}

persistFilter();
persistSort();
loadTasks();
