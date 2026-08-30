"use strict";

const ui = {
  shell: document.querySelector("#app"), projectList: document.querySelector("#project-list"),
  taskList: document.querySelector("#task-list"), title: document.querySelector("#conversation-title"),
  status: document.querySelector("#run-status"), workspace: document.querySelector("#workspace-button"),
  transcript: document.querySelector("#transcript"), empty: document.querySelector("#empty-state"),
  composer: document.querySelector("#composer-form"), input: document.querySelector("#message-input"),
  send: document.querySelector("#send-message"), stop: document.querySelector("#stop-task"),
  composerWorkspace: document.querySelector("#composer-workspace"),
  permissionMode: document.querySelector("#permission-mode"),
  connection: document.querySelector("#connection-label"), diffPanel: document.querySelector("#diff-panel"),
  diffPath: document.querySelector("#diff-path"), diffCounts: document.querySelector("#diff-counts"),
  diffWarning: document.querySelector("#diff-warning"), diffContent: document.querySelector("#diff-content"),
  workspaceDialog: document.querySelector("#workspace-dialog"), workspaceForm: document.querySelector("#workspace-form"),
  workspacePath: document.querySelector("#workspace-path"), workspaceTitle: document.querySelector("#workspace-dialog-title"),
  browseWorkspace: document.querySelector("#browse-workspace"),
  workspaceKicker: document.querySelector("#workspace-dialog-kicker"), settingsDialog: document.querySelector("#settings-dialog"),
  settingsForm: document.querySelector("#settings-form"), renameDialog: document.querySelector("#rename-dialog"),
  renameForm: document.querySelector("#rename-form"), renameValue: document.querySelector("#rename-value"),
  approvalDialog: document.querySelector("#approval-dialog"), approvalReason: document.querySelector("#approval-reason"),
  approvalCommand: document.querySelector("#approval-command"), toast: document.querySelector("#toast"),
  deleteDialog: document.querySelector("#delete-dialog"), deleteForm: document.querySelector("#delete-form"),
  deleteName: document.querySelector("#delete-name"), deleteDescription: document.querySelector("#delete-description"),
};

let state = { projects: [], tasks: [], approvals: [], settings: {}, current_id: null };
let workspaceMode = "project";
let renameTarget = null;
let deleteTarget = null;
let menuEl = null;
let menuAnchor = null;
let shownApproval = null;
let lastTranscriptKey = "";
let toastTimer = null;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function appendInlineMarkdown(parent, text) {
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > cursor) parent.append(document.createTextNode(text.slice(cursor, match.index)));
    const token = match[0];
    if (token.startsWith("`")) {
      parent.append(element("code", "md-inline-code", token.slice(1, -1)));
    } else if (token.startsWith("**") || token.startsWith("__")) {
      parent.append(element("strong", "", token.slice(2, -2)));
    } else if (token.startsWith("*")) {
      parent.append(element("em", "", token.slice(1, -1)));
    } else {
      const split = token.lastIndexOf("](");
      const link = element("a", "", token.slice(1, split));
      link.href = token.slice(split + 2, -1);
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      parent.append(link);
    }
    cursor = match.index + token.length;
  }
  if (cursor < text.length) parent.append(document.createTextNode(text.slice(cursor)));
}

function markdownCells(line) {
  let value = line.trim();
  if (value.startsWith("|")) value = value.slice(1);
  if (value.endsWith("|")) value = value.slice(0, -1);
  return value.split("|").map((cell) => cell.trim());
}

function isTableDivider(line) {
  const cells = markdownCells(line);
  return cells.length > 1 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function startsMarkdownBlock(lines, index) {
  const line = lines[index] || "";
  return /^```/.test(line) || /^(#{1,4})\s+/.test(line) || /^\s*([-+*]|\d+[.)])\s+/.test(line)
    || /^>\s?/.test(line) || (line.includes("|") && isTableDivider(lines[index + 1] || ""));
}

function renderMarkdown(text, root) {
  root.classList.add("markdown");
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  let index = 0;
  while (index < lines.length) {
    if (!lines[index].trim()) { index += 1; continue; }

    const fence = lines[index].match(/^```\s*([\w.+-]*)\s*$/);
    if (fence) {
      const language = fence[1] || "text";
      const content = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) content.push(lines[index++]);
      if (index < lines.length) index += 1;
      const block = element("div", "md-code-block");
      block.append(element("div", "md-code-head", language), element("pre", "", content.join("\n")));
      root.append(block);
      continue;
    }

    const heading = lines[index].match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const node = element(`h${Math.min(heading[1].length + 1, 5)}`, "");
      appendInlineMarkdown(node, heading[2]);
      root.append(node); index += 1; continue;
    }

    if (lines[index].includes("|") && isTableDivider(lines[index + 1] || "")) {
      const table = element("table", "md-table");
      const head = element("thead", "");
      const headRow = element("tr", "");
      for (const cell of markdownCells(lines[index])) {
        const th = element("th", ""); appendInlineMarkdown(th, cell); headRow.append(th);
      }
      head.append(headRow); table.append(head); index += 2;
      const body = element("tbody", "");
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        const row = element("tr", "");
        for (const cell of markdownCells(lines[index])) {
          const td = element("td", ""); appendInlineMarkdown(td, cell); row.append(td);
        }
        body.append(row); index += 1;
      }
      table.append(body); root.append(table); continue;
    }

    const listMatch = lines[index].match(/^\s*([-+*]|\d+[.)])\s+(.+)$/);
    if (listMatch) {
      const ordered = /^\d/.test(listMatch[1]);
      const list = element(ordered ? "ol" : "ul", "");
      while (index < lines.length) {
        const item = lines[index].match(/^\s*([-+*]|\d+[.)])\s+(.+)$/);
        if (!item || /^\d/.test(item[1]) !== ordered) break;
        const li = element("li", ""); appendInlineMarkdown(li, item[2]); list.append(li); index += 1;
      }
      root.append(list); continue;
    }

    if (/^>\s?/.test(lines[index])) {
      const quoteLines = [];
      while (index < lines.length && /^>\s?/.test(lines[index])) quoteLines.push(lines[index++].replace(/^>\s?/, ""));
      const quote = element("blockquote", ""); appendInlineMarkdown(quote, quoteLines.join(" ")); root.append(quote); continue;
    }

    const paragraphLines = [lines[index++].trim()];
    while (index < lines.length && lines[index].trim() && !startsMarkdownBlock(lines, index)) {
      paragraphLines.push(lines[index++].trim());
    }
    const paragraph = element("p", "");
    appendInlineMarkdown(paragraph, paragraphLines.join(" "));
    root.append(paragraph);
  }
}

async function api(path, options = {}) {
  const request = { method: options.method || "GET", headers: {} };
  if (options.body !== undefined) {
    request.headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, request);
  const raw = await response.text();
  let data = {};
  if (raw) {
    try { data = JSON.parse(raw); } catch { throw new Error("本机服务返回了无法解析的内容"); }
  }
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

function toast(message) {
  ui.toast.textContent = message;
  ui.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { ui.toast.hidden = true; }, 3500);
}

function currentTask() { return state.tasks.find((task) => task.id === state.current_id) || state.tasks[0] || null; }
function currentProject() {
  const task = currentTask();
  return task ? state.projects.find((project) => project.id === task.project_id) || null : null;
}

function taskNode(task) {
  const item = element("div", `task-item${task.id === state.current_id ? " active" : ""}${task.running ? " running" : ""}`);
  const select = element("button", "task-select", task.title);
  select.type = "button";
  select.title = task.title;
  select.addEventListener("click", () => selectTask(task.id));
  const menu = element("button", "mini-menu", "•••");
  menu.type = "button";
  menu.setAttribute("aria-label", `对话菜单 ${task.title}`);
  menu.addEventListener("click", (event) => { event.stopPropagation(); openItemMenu("task", task.id, task.title, menu); });
  item.append(select, menu);
  return item;
}

function renderSidebar() {
  ui.projectList.replaceChildren();
  ui.taskList.replaceChildren();
  for (const project of state.projects) {
    const group = element("section", "project-group");
    const heading = element("div", "project-title");
    const name = element("span", "", project.title);
    name.title = project.path;
    const menu = element("button", "mini-menu", "•••");
    menu.type = "button";
    menu.setAttribute("aria-label", `项目菜单 ${project.title}`);
    menu.addEventListener("click", (event) => { event.stopPropagation(); openItemMenu("project", project.id, project.title, menu); });
    heading.append(name, menu);
    const tasks = element("div", "task-list");
    for (const task of state.tasks.filter((item) => item.project_id === project.id)) tasks.append(taskNode(task));
    group.append(heading, tasks);
    ui.projectList.append(group);
  }
  for (const task of state.tasks.filter((item) => !item.project_id)) ui.taskList.append(taskNode(task));
}

function messageNode(entry) {
  const article = element("article", `message ${entry.kind}`);
  const labels = { assistant: "小码", tool: "本地工具", error: "运行错误", system: "系统" };
  article.append(element("p", "message-label", labels[entry.kind] || "你"));
  const body = element("div", "message-body");
  if (entry.kind === "assistant") renderMarkdown(entry.text, body);
  else body.textContent = entry.text;
  article.append(body);
  if (entry.change_paths && entry.change_paths.length) {
    const summary = element("div", "change-summary");
    summary.append(element("p", "change-summary-title", `本轮改动 · ${entry.change_paths.length} 个文件`));
    for (const path of entry.change_paths) {
      const card = element("button", "change-card");
      card.type = "button";
      card.append(element("span", "change-icon", "▤"), element("span", "change-path", path), element("span", "", "打开 →"));
      card.addEventListener("click", () => openDiff(path));
      summary.append(card);
    }
    article.append(summary);
  }
  return article;
}

function renderConversation() {
  const task = currentTask();
  if (!task) {
    ui.title.textContent = "新对话"; ui.status.textContent = "就绪";
    ui.workspace.textContent = "尚未选择工作目录"; ui.empty.hidden = false;
    ui.transcript.replaceChildren(); ui.send.disabled = false; ui.stop.hidden = true;
    ui.workspace.disabled = false; ui.composerWorkspace.disabled = false;
    ui.permissionMode.value = state.settings.approval_mode || "risk";
    ui.permissionMode.disabled = true;
    return;
  }
  ui.title.textContent = task.title;
  ui.status.textContent = task.status;
  ui.status.classList.toggle("running", task.running);
  ui.workspace.textContent = task.workspace || "尚未选择工作目录";
  ui.workspace.title = task.workspace || "为这段对话选择工作目录";
  ui.send.disabled = task.running;
  ui.workspace.disabled = task.running;
  ui.composerWorkspace.disabled = task.running;
  ui.permissionMode.value = task.permission_mode || state.settings.approval_mode || "risk";
  ui.permissionMode.disabled = task.running;
  ui.stop.hidden = !task.running;
  ui.empty.hidden = task.entries.length > 0;
  const last = task.entries.length ? task.entries[task.entries.length - 1].text : "";
  const key = `${task.id}:${task.entries.length}:${last}:${task.running}`;
  if (key !== lastTranscriptKey) {
    ui.transcript.replaceChildren(...task.entries.map(messageNode));
    ui.transcript.scrollTop = ui.transcript.scrollHeight;
    lastTranscriptKey = key;
  }
}

function renderSettingsStatus() {
  ui.connection.textContent = state.settings.api_key_configured
    ? `${state.settings.model || "模型"} · 凭据已配置`
    : "需要配置 API Key";
}

function renderApproval() {
  const approval = state.approvals[0];
  if (!approval) {
    shownApproval = null;
    if (ui.approvalDialog.open) ui.approvalDialog.close();
    return;
  }
  if (approval.id === shownApproval) return;
  shownApproval = approval.id;
  ui.approvalReason.textContent = `${approval.reason} · 风险级别 ${approval.risk}`;
  ui.approvalCommand.textContent = approval.command;
  if (!ui.approvalDialog.open) ui.approvalDialog.showModal();
}

function render() { renderSidebar(); renderConversation(); renderSettingsStatus(); renderApproval(); }

async function refresh(silent = true) {
  try {
    state = await api("/api/state");
    if (!state.current_id && state.tasks.length) state.current_id = state.tasks[0].id;
    render();
  } catch (error) {
    if (!silent) toast(error.message);
    ui.connection.textContent = "本机服务连接失败";
  }
}

async function selectTask(taskId) {
  try {
    await api(`/api/conversations/${taskId}/select`, { method: "POST", body: {} });
    closeSidebar(); closeDiff(); await refresh(false);
  } catch (error) { toast(error.message); }
}

async function newConversation() {
  const project = currentProject();
  try {
    const data = await api("/api/conversations", { method: "POST", body: { project_id: project ? project.id : null } });
    state.current_id = data.task.id;
    closeSidebar(); closeDiff(); await refresh(false); ui.input.focus();
  } catch (error) { toast(error.message); }
}

async function changePermissionMode() {
  const task = currentTask();
  if (!task) return;
  const labels = { request: "请求批准", risk: "帮我批准", full: "完全访问权限" };
  try {
    await api(`/api/conversations/${task.id}/permission`, {
      method: "POST", body: { mode: ui.permissionMode.value },
    });
    toast(`当前对话已切换为“${labels[ui.permissionMode.value]}”`);
    await refresh(false);
  } catch (error) {
    await refresh(true);
    toast(error.message);
  }
}

function openWorkspace(mode) {
  workspaceMode = mode;
  const task = currentTask();
  if (mode === "task" && task && task.running) { toast("任务运行时不能更改工作目录"); return; }
  ui.workspaceKicker.textContent = mode === "project" ? "本机项目" : "当前对话";
  ui.workspaceTitle.textContent = mode === "project" ? "添加项目" : "选择工作目录";
  ui.workspacePath.value = mode === "task" && task && task.workspace ? task.workspace : "";
  ui.workspaceDialog.showModal();
  ui.workspacePath.focus();
}

async function submitWorkspace(event) {
  event.preventDefault();
  const path = ui.workspacePath.value.trim();
  try {
    if (workspaceMode === "project") {
      const projectData = await api("/api/projects", { method: "POST", body: { path } });
      const taskData = await api("/api/conversations", { method: "POST", body: { project_id: projectData.project.id } });
      state.current_id = taskData.task.id;
    } else {
      const task = currentTask();
      if (!task) throw new Error("请先新建对话");
      await api(`/api/conversations/${task.id}/workspace`, { method: "POST", body: { path } });
    }
    ui.workspaceDialog.close(); await refresh(false);
  } catch (error) { toast(error.message); }
}

async function browseWorkspace() {
  const task = currentTask();
  if (workspaceMode === "task" && !task) { toast("请先新建对话"); return; }
  const initial = ui.workspacePath.value.trim() || (task && task.workspace) || null;
  const path = workspaceMode === "project"
    ? "/api/projects/pick"
    : `/api/conversations/${task.id}/pick-workspace`;
  const previous = ui.browseWorkspace.textContent;
  ui.browseWorkspace.disabled = true;
  ui.browseWorkspace.textContent = "正在选择…";
  try {
    const data = await api(path, { method: "POST", body: { initial } });
    if (data.cancelled) return;
    if (data.task) state.current_id = data.task.id;
    ui.workspaceDialog.close();
    closeDiff();
    await refresh(false);
  } catch (error) { toast(error.message); }
  finally {
    ui.browseWorkspace.disabled = false;
    ui.browseWorkspace.textContent = previous;
  }
}

async function sendMessage(event) {
  event.preventDefault();
  let task = currentTask();
  const content = ui.input.value.trim();
  if (!content) return;
  try {
    if (!task) {
      const created = await api("/api/conversations", { method: "POST", body: {} });
      task = created.task; state.current_id = task.id;
    }
    await api(`/api/conversations/${task.id}/messages`, { method: "POST", body: { content } });
    ui.input.value = ""; await refresh(false);
  } catch (error) { toast(error.message); }
}

async function stopTask() {
  const task = currentTask();
  if (!task) return;
  try { await api(`/api/conversations/${task.id}/cancel`, { method: "POST", body: {} }); await refresh(false); }
  catch (error) { toast(error.message); }
}

async function openDiff(path) {
  const task = currentTask();
  if (!task) return;
  try {
    const data = await api(`/api/conversations/${task.id}/changes/${encodeURIComponent(path)}`);
    const change = data.change;
    ui.diffPath.textContent = change.path; ui.diffPath.title = change.path;
    ui.diffCounts.replaceChildren(element("b", "", `+${change.added}`), element("i", "", `−${change.deleted}`));
    ui.diffWarning.hidden = !change.warning; ui.diffWarning.textContent = change.warning || "";
    ui.diffContent.replaceChildren(...change.rows.map(diffRow));
    ui.shell.classList.add("diff-open"); ui.diffPanel.setAttribute("aria-hidden", "false");
  } catch (error) { toast(error.message); }
}

function diffRow(row) {
  const line = element("div", `diff-row ${row.kind}`);
  line.setAttribute("role", "row");
  if (["hunk", "segment", "warning"].includes(row.kind)) { line.textContent = row.text; return line; }
  const marker = row.kind === "added" ? "+" : row.kind === "removed" ? "−" : " ";
  line.append(element("span", "line", row.old_line ?? ""), element("span", "line", row.new_line ?? ""), element("span", "marker", marker), element("span", "code", row.text));
  return line;
}

function closeDiff() { ui.shell.classList.remove("diff-open"); ui.diffPanel.setAttribute("aria-hidden", "true"); }
function openRename(kind, id, title) {
  renameTarget = { kind, id }; ui.renameValue.value = title; ui.renameDialog.showModal(); ui.renameValue.select();
}
async function submitRename(event) {
  event.preventDefault();
  if (!renameTarget) return;
  const base = renameTarget.kind === "project" ? "projects" : "conversations";
  try {
    await api(`/api/${base}/${renameTarget.id}`, { method: "PATCH", body: { title: ui.renameValue.value } });
    ui.renameDialog.close(); await refresh(false);
  } catch (error) { toast(error.message); }
}

function itemMenuElement() {
  if (menuEl) return menuEl;
  menuEl = element("div", "item-menu");
  menuEl.setAttribute("role", "menu");
  menuEl.hidden = true;
  document.body.append(menuEl);
  document.addEventListener("click", (event) => {
    if (!menuEl.hidden && !menuEl.contains(event.target) && !menuAnchor?.contains(event.target)) closeItemMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeItemMenu();
  });
  return menuEl;
}

function closeItemMenu() {
  if (menuEl) menuEl.hidden = true;
  if (menuAnchor) menuAnchor.setAttribute("aria-expanded", "false");
  menuAnchor = null;
}

function openItemMenu(kind, id, title, anchor) {
  const menu = itemMenuElement();
  if (!menu.hidden && menuAnchor === anchor) { closeItemMenu(); return; }
  closeItemMenu();
  menuAnchor = anchor;
  anchor.setAttribute("aria-haspopup", "menu");
  anchor.setAttribute("aria-expanded", "true");
  menu.replaceChildren();
  const rename = element("button", "", "重命名");
  rename.type = "button";
  rename.setAttribute("role", "menuitem");
  rename.addEventListener("click", () => { closeItemMenu(); openRename(kind, id, title); });
  const remove = element("button", "danger", "删除");
  remove.type = "button";
  remove.setAttribute("role", "menuitem");
  remove.addEventListener("click", () => { closeItemMenu(); openDelete(kind, id, title); });
  menu.append(rename, remove);
  menu.hidden = false;
  const rect = anchor.getBoundingClientRect();
  const left = Math.max(8, Math.min(rect.right - menu.offsetWidth, window.innerWidth - menu.offsetWidth - 8));
  const below = rect.bottom + 6;
  const top = below + menu.offsetHeight <= window.innerHeight - 8
    ? below : Math.max(8, rect.top - menu.offsetHeight - 6);
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  rename.focus();
}

function openDelete(kind, id, title) {
  deleteTarget = { kind, id, title };
  ui.deleteName.textContent = title;
  ui.deleteDescription.textContent = kind === "project"
    ? "删除项目后，其中的对话会保留并移到「未选择工作目录」，本地文件不受影响。"
    : "删除后这段对话及其记录将无法恢复，本地文件不受影响。";
  ui.deleteDialog.showModal();
}

async function submitDelete(event) {
  event.preventDefault();
  if (!deleteTarget) return;
  const base = deleteTarget.kind === "project" ? "projects" : "conversations";
  const wasProject = deleteTarget.kind === "project";
  try {
    await api(`/api/${base}/${deleteTarget.id}`, { method: "DELETE" });
    deleteTarget = null;
    ui.deleteDialog.close();
    closeDiff();
    toast(wasProject ? "项目已删除，对话已保留" : "对话已删除");
    await refresh(false);
  } catch (error) { toast(error.message); }
}

function openSettings() {
  const settings = state.settings;
  document.querySelector("#setting-api-key").value = "";
  document.querySelector("#setting-model").value = settings.model || "";
  document.querySelector("#setting-base-url").value = settings.base_url || "";
  document.querySelector("#setting-context").value = settings.context_tokens || 32000;
  document.querySelector("#setting-steps").value = settings.max_steps || 20;
  document.querySelector("#setting-approval").value = settings.approval_mode || "risk";
  document.querySelector("#setting-remember").checked = Boolean(settings.remember_key);
  ui.settingsDialog.showModal();
}

async function saveSettings(event) {
  event.preventDefault();
  const form = new FormData(ui.settingsForm);
  const body = {
    api_key: String(form.get("api_key") || ""), model: String(form.get("model") || ""),
    base_url: String(form.get("base_url") || ""), context_tokens: Number(form.get("context_tokens")),
    max_steps: Number(form.get("max_steps")), approval_mode: String(form.get("approval_mode") || "risk"),
    remember_key: form.get("remember_key") === "on",
  };
  try {
    await api("/api/settings", { method: "POST", body });
    ui.settingsDialog.close(); toast("设置已保存到本机"); await refresh(false);
  } catch (error) { toast(error.message); }
}

async function resolveApproval(approved) {
  if (!shownApproval) return;
  try {
    await api(`/api/approvals/${shownApproval}`, { method: "POST", body: { approved } });
    ui.approvalDialog.close(); shownApproval = null; await refresh(false);
  } catch (error) { toast(error.message); }
}

function openSidebar() { document.body.classList.add("sidebar-open"); }
function closeSidebar() { document.body.classList.remove("sidebar-open"); }

document.querySelector("#new-conversation").addEventListener("click", newConversation);
document.querySelector("#add-project").addEventListener("click", () => openWorkspace("project"));
document.querySelector("#workspace-button").addEventListener("click", () => openWorkspace("task"));
document.querySelector("#composer-workspace").addEventListener("click", () => openWorkspace("task"));
document.querySelector("#open-settings").addEventListener("click", openSettings);
document.querySelector("#rename-current").addEventListener("click", (event) => {
  event.stopPropagation();
  const task = currentTask();
  if (task) openItemMenu("task", task.id, task.title, event.currentTarget);
});
document.querySelector("#close-diff").addEventListener("click", closeDiff);
document.querySelector("#open-sidebar").addEventListener("click", openSidebar);
document.querySelector("#close-sidebar").addEventListener("click", closeSidebar);
document.querySelector("#sidebar-scrim").addEventListener("click", closeSidebar);
document.querySelector("#stop-task").addEventListener("click", stopTask);
document.querySelector("#approve-command").addEventListener("click", () => resolveApproval(true));
document.querySelector("#deny-command").addEventListener("click", () => resolveApproval(false));
ui.workspaceForm.addEventListener("submit", submitWorkspace);
ui.browseWorkspace.addEventListener("click", browseWorkspace);
ui.composer.addEventListener("submit", sendMessage);
ui.renameForm.addEventListener("submit", submitRename);
ui.deleteForm.addEventListener("submit", submitDelete);
ui.settingsForm.addEventListener("submit", saveSettings);
ui.permissionMode.addEventListener("change", changePermissionMode);
ui.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); ui.composer.requestSubmit(); }
});
for (const button of document.querySelectorAll("#suggestions button")) {
  button.addEventListener("click", () => { ui.input.value = button.textContent; ui.input.focus(); });
}
for (const button of document.querySelectorAll(".dialog-close")) {
  button.addEventListener("click", () => button.closest("dialog").close());
}

refresh(false).then(async () => {
  if (!state.tasks.length) {
    try { await api("/api/conversations", { method: "POST", body: {} }); await refresh(false); }
    catch (error) { toast(error.message); }
  }
});
setInterval(() => refresh(true), 700);
