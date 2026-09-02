"use strict";

const ui = {
  shell: document.querySelector("#app"), projectList: document.querySelector("#project-list"),
  taskList: document.querySelector("#task-list"), title: document.querySelector("#conversation-title"),
  status: document.querySelector("#run-status"), workspace: document.querySelector("#workspace-button"),
  progress: document.querySelector("#operation-progress"), progressKind: document.querySelector("#operation-kind"),
  progressLabel: document.querySelector("#operation-label"), progressMeta: document.querySelector("#operation-meta"),
  progressPercent: document.querySelector("#operation-percent"), progressStages: document.querySelector("#operation-stages"),
  progressMeter: document.querySelector("#operation-meter"), progressFill: document.querySelector("#operation-meter-fill"),
  cancelOperation: document.querySelector("#cancel-operation"),
  taskPlan: document.querySelector("#task-plan"), taskPlanObjective: document.querySelector("#task-plan-objective"),
  taskPlanCompleted: document.querySelector("#task-plan-completed"), taskPlanBlocked: document.querySelector("#task-plan-blocked"),
  taskPlanMeterFill: document.querySelector("#task-plan-meter-fill"), taskPlanItems: document.querySelector("#task-plan-items"),
  transcript: document.querySelector("#transcript"), empty: document.querySelector("#empty-state"),
  composer: document.querySelector("#composer-form"), input: document.querySelector("#message-input"),
  send: document.querySelector("#send-message"), stop: document.querySelector("#stop-task"),
  composerWorkspace: document.querySelector("#composer-workspace"),
  permissionMode: document.querySelector("#permission-mode"),
  connection: document.querySelector("#connection-label"), diffPanel: document.querySelector("#diff-panel"),
  diffPath: document.querySelector("#diff-path"), diffCounts: document.querySelector("#diff-counts"),
  diffWarning: document.querySelector("#diff-warning"), diffContent: document.querySelector("#diff-content"),
  diffView: document.querySelector("#diff-view"), operationsView: document.querySelector("#operations-view"),
  operationsContent: document.querySelector("#operations-content"),
  refreshOperations: document.querySelector("#refresh-operations"),
  worktreeButton: document.querySelector("#create-worktree"),
  worktreeDialog: document.querySelector("#worktree-dialog"),
  worktreeForm: document.querySelector("#worktree-form"),
  worktreeSource: document.querySelector("#worktree-source"),
  worktreeBranchPreview: document.querySelector("#worktree-branch-preview"),
  confirmWorktree: document.querySelector("#confirm-worktree"),
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
let lastSidebarKey = "";
let transcriptTaskId = null;
let renderedEntryKeys = [];
let streamingMessage = null;
let lastStreamingText = "";
let stateEvents = null;
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
  const sidebarKey = JSON.stringify({
    current: state.current_id,
    projects: state.projects.map((project) => [project.id, project.title, project.path]),
    tasks: state.tasks.map((task) => [task.id, task.project_id, task.title, task.running]),
  });
  if (sidebarKey === lastSidebarKey) return;
  lastSidebarKey = sidebarKey;
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

function messageNode(entry, options = {}) {
  const article = element("article", `message ${entry.kind}`);
  if (entry.streaming) article.classList.add("streaming");
  if (entry.kind === "decision_summary") {
    const card = element("details", "decision-card");
    card.dataset.entryId = entry.id || "";
    card.open = Boolean(options.expanded);
    const head = element("summary", "decision-head");
    const marker = element("span", "decision-marker");
    const heading = element("span", "decision-heading");
    const decisionNumber = options.decisionNumber || entry.step;
    heading.append(
      element("strong", "", `决策摘要${decisionNumber ? ` ${decisionNumber}` : ""}`),
      element("small", "", (entry.tools || []).join(" · ") || "准备下一步行动"),
    );
    const toggle = element("span", "decision-toggle");
    const syncDecisionState = () => {
      marker.textContent = card.open ? "▸" : "✓";
      toggle.textContent = card.open ? "收起" : "展开";
    };
    syncDecisionState();
    card.addEventListener("toggle", syncDecisionState);
    head.append(marker, heading, toggle);
    card.append(head, element("div", "decision-body", entry.text));
    article.append(card);
    return article;
  }
  const labels = { assistant: "小码", tool: "本地工具", error: "运行错误", system: "系统" };
  article.append(element("p", "message-label", labels[entry.kind] || "你"));
  const body = element("div", "message-body");
  if (entry.kind === "assistant") renderMarkdown(entry.text, body);
  else body.textContent = entry.text;
  article.append(body);
  if (entry.change_paths && entry.change_paths.length) {
    const summary = element("div", "change-summary");
    const scopeLabel = entry.change_scope === "conversation" ? "对话累计改动（旧记录）" : "本轮改动";
    summary.append(element("p", "change-summary-title", `${scopeLabel} · ${entry.change_paths.length} 个文件`));
    for (const path of entry.change_paths) {
      const card = element("button", "change-card");
      card.type = "button";
      card.append(element("span", "change-icon", "▤"), element("span", "change-path", path), element("span", "", "打开 →"));
      card.addEventListener("click", () => openDiff(path, entry.id));
      summary.append(card);
    }
    article.append(summary);
  }
  return article;
}

const localToolLabels = {
  list_files: "浏览文件",
  read_file: "读取文件",
  search_text: "搜索代码",
  write_file: "写入文件",
  batch_write_files: "批量写入",
  replace_text: "修改文件",
  batch_replace_text: "批量修改",
  run_command: "执行命令",
  run_process: "运行进程",
  load_skill: "加载 Skill",
  read_skill_resource: "读取 Skill 资源",
  run_skill_script: "运行 Skill 脚本",
  mcp_status: "检查 MCP",
  mcp_list_resources: "列出 MCP 资源",
  mcp_read_resource: "读取 MCP 资源",
  mcp_list_prompts: "列出 MCP 提示词",
  mcp_get_prompt: "读取 MCP 提示词",
  git_status: "检查 Git 状态",
  git_diff: "查看代码差异",
  git_log: "查看提交记录",
  git_branches: "查看分支",
  git_create_branch: "创建分支",
  git_stage: "暂存文件",
  git_unstage: "取消暂存",
  git_commit: "提交代码",
  git_pull: "拉取代码",
  git_push: "推送代码",
};

function toolEntryParts(entry) {
  const lines = String(entry.text || "").split(/\r?\n/);
  const name = (lines.shift() || "工具").trim();
  const detail = lines.join("\n").trim();
  const compact = detail.replace(/\s+/g, " ").trim();
  return {
    name,
    label: localToolLabels[name]
      || (name.startsWith("git_") ? "Git 操作" : name.startsWith("mcp_") ? "MCP 调用" : name),
    detail,
    preview: compact ? `${compact.slice(0, 108)}${compact.length > 108 ? "…" : ""}` : "已完成",
  };
}

function toolGroupNode(entries) {
  const article = element("article", "execution-group");
  article.dataset.toolStart = entries[0] && entries[0].id || "";
  const parsed = entries.map(toolEntryParts);
  const counts = new Map();
  for (const item of parsed) counts.set(item.label, (counts.get(item.label) || 0) + 1);
  const digest = [...counts.entries()].map(([label, count]) => `${label}${count > 1 ? ` ×${count}` : ""}`).join(" · ");
  const head = element("div", "execution-head");
  head.append(
    element("span", "execution-marker", "✓"),
    element("strong", "", "本地执行"),
    element("small", "", `${entries.length} 项 · ${digest}`),
  );
  const list = element("div", "execution-list");
  for (const item of parsed) {
    const row = element("details", "execution-item");
    const summary = element("summary", "execution-row");
    const name = element("span", "execution-name", item.label);
    name.title = item.name;
    summary.append(
      name,
      element("span", "execution-preview", item.preview),
      element("span", "execution-more", item.detail ? "详情" : ""),
    );
    row.append(summary);
    if (item.detail) row.append(element("pre", "execution-detail", item.detail));
    list.append(row);
  }
  article.append(head, list);
  return article;
}

function transcriptNodes(entries, latestDecisionId) {
  const nodes = [];
  let decisionNumber = 0;
  for (let index = 0; index < entries.length;) {
    const entry = entries[index];
    if (entry.kind === "tool") {
      let end = index + 1;
      while (end < entries.length && entries[end].kind === "tool") end += 1;
      nodes.push(toolGroupNode(entries.slice(index, end)));
      index = end;
      continue;
    }
    if (entry.kind === "decision_summary") decisionNumber += 1;
    nodes.push(messageNode(entry, {
      expanded: entry.id === latestDecisionId,
      decisionNumber: entry.kind === "decision_summary" ? decisionNumber : undefined,
    }));
    index += 1;
  }
  return nodes;
}

function decisionNumberAt(entries, targetIndex) {
  let number = 0;
  for (let index = 0; index <= targetIndex; index += 1) {
    if (entries[index].kind === "decision_summary") number += 1;
  }
  return number;
}

function appendTranscriptEntries(entries, startIndex, latestDecisionId) {
  for (let index = startIndex; index < entries.length;) {
    const entry = entries[index];
    if (entry.kind === "tool") {
      let groupStart = index;
      while (groupStart > 0 && entries[groupStart - 1].kind === "tool") groupStart -= 1;
      let groupEnd = index + 1;
      while (groupEnd < entries.length && entries[groupEnd].kind === "tool") groupEnd += 1;
      const replacement = toolGroupNode(entries.slice(groupStart, groupEnd));
      const startId = entries[groupStart].id || "";
      const existing = [...ui.transcript.querySelectorAll(".execution-group")]
        .find((node) => node.dataset.toolStart === startId);
      if (existing) existing.replaceWith(replacement);
      else ui.transcript.append(replacement);
      index = groupEnd;
      continue;
    }
    ui.transcript.append(messageNode(entry, {
      expanded: entry.id === latestDecisionId,
      decisionNumber: entry.kind === "decision_summary" ? decisionNumberAt(entries, index) : undefined,
    }));
    index += 1;
  }
}

function transcriptEntryKey(entry, index) {
  return entry.id || `${index}:${entry.kind}:${entry.text}`;
}

function isTranscriptNearBottom() {
  return ui.transcript.scrollHeight - ui.transcript.scrollTop - ui.transcript.clientHeight < 96;
}

function updateStreamingMessage(text) {
  if (!streamingMessage || !streamingMessage.isConnected) {
    streamingMessage = messageNode({
      kind: "assistant", text, change_paths: [], streaming: true,
    });
    ui.transcript.append(streamingMessage);
  } else if (text !== lastStreamingText) {
    const body = streamingMessage.querySelector(".message-body");
    body.replaceChildren();
    renderMarkdown(text, body);
  }
  lastStreamingText = text;
}

function renderTranscript(task, streaming) {
  const shouldFollow = transcriptTaskId !== task.id || isTranscriptNearBottom();
  const nextKeys = task.entries.map(transcriptEntryKey);
  const latestDecision = [...task.entries].reverse().find((entry) => entry.kind === "decision_summary");
  const latestDecisionId = latestDecision && latestDecision.id;
  const canAppend = transcriptTaskId === task.id
    && renderedEntryKeys.length <= nextKeys.length
    && renderedEntryKeys.every((key, index) => key === nextKeys[index]);

  if (!canAppend) {
    ui.transcript.replaceChildren(...transcriptNodes(task.entries, latestDecisionId));
    streamingMessage = null;
    lastStreamingText = "";
  } else if (nextKeys.length > renderedEntryKeys.length) {
    const additions = task.entries.slice(renderedEntryKeys.length);
    if (additions.some((entry) => entry.kind === "decision_summary")) {
      for (const card of ui.transcript.querySelectorAll(".decision-card[open]")) card.open = false;
    }
    if (streamingMessage) streamingMessage.remove();
    streamingMessage = null;
    lastStreamingText = "";
    appendTranscriptEntries(task.entries, renderedEntryKeys.length, latestDecisionId);
  }

  transcriptTaskId = task.id;
  renderedEntryKeys = nextKeys;
  if (streaming) updateStreamingMessage(streaming);
  else if (streamingMessage) {
    streamingMessage.remove();
    streamingMessage = null;
    lastStreamingText = "";
  }
  if (shouldFollow) ui.transcript.scrollTop = ui.transcript.scrollHeight;
}

const operationLabels = {
  compose_preflight: "PRE-FLIGHT", compose_status: "STATUS", compose_logs: "LOGS",
  compose_build: "BUILD", compose_pull: "PULL", compose_deploy: "DEPLOY",
  compose_release: "RELEASE", compose_rollback: "ROLLBACK",
  compose_verify: "VERIFY", compose_restart: "RESTART", compose_stop: "STOP",
};

const operationPhases = {
  compose_preflight: ["连接引擎", "检查版本", "校验配置", "服务清单"],
  compose_deploy: ["校验配置", "构建启动", "健康验证"],
  compose_release: ["发布门禁", "校验配置", "构建启动", "健康验证", "锁定镜像", "发布记录"],
  compose_rollback: ["确认计划", "现场快照", "恢复镜像", "重建服务", "健康验证"],
};

function renderProgress(task) {
  const progress = task && task.progress;
  if (!progress) { ui.progress.hidden = true; return; }
  ui.progress.hidden = false;
  ui.progress.dataset.state = progress.state || "running";
  ui.progressKind.textContent = operationLabels[progress.operation] || "DEVOPS";
  ui.progressLabel.textContent = progress.label || "执行部署操作";
  const seconds = Number(progress.elapsed_seconds || 0).toFixed(1);
  ui.progressMeta.textContent = `${progress.environment || "默认环境"} · ${seconds}s`;
  const percent = Math.max(0, Math.min(Number(progress.percent || 0), 100));
  ui.progressPercent.textContent = `${percent}%`;
  ui.progressMeter.setAttribute("aria-valuenow", String(percent));
  ui.progressMeter.setAttribute("aria-valuetext", `${progress.label || "部署"}，${percent}%`);
  ui.progressFill.style.width = `${percent}%`;

  const fallback = Array.from({ length: Math.max(1, Number(progress.total || 1)) }, (_, index) => `步骤 ${index + 1}`);
  const phases = operationPhases[progress.operation] || fallback;
  ui.progressStages.style.setProperty("--stage-count", String(phases.length));
  ui.progressStages.replaceChildren(...phases.map((label, index) => {
    const number = index + 1;
    let stageState = number < progress.current ? "completed" : number === progress.current ? "active" : "pending";
    if (progress.state === "completed") stageState = "completed";
    if (["failed", "cancelled"].includes(progress.state) && number === progress.current) stageState = progress.state;
    const stage = element("span", `operation-stage ${stageState}`);
    stage.append(element("i", "", stageState === "completed" ? "✓" : String(number)), element("b", "", label));
    return stage;
  }));
  const terminal = ["completed", "failed", "cancelled"].includes(progress.state);
  ui.cancelOperation.hidden = terminal || !task.running;
  ui.cancelOperation.disabled = progress.state === "cancelling";
  ui.cancelOperation.textContent = progress.state === "cancelling" ? "正在停止…" : "取消部署";
}

function renderTaskPlan(task) {
  const plan = task && task.task_list;
  if (!plan || !plan.total) { ui.taskPlan.hidden = true; return; }
  ui.taskPlan.hidden = false;
  ui.taskPlanObjective.textContent = plan.objective;
  ui.taskPlanCompleted.textContent = `${plan.completed}/${plan.total}`;
  ui.taskPlanBlocked.hidden = !plan.blocked;
  ui.taskPlanBlocked.textContent = plan.blocked ? `${plan.blocked} 项阻塞` : "";
  ui.taskPlanMeterFill.style.width = `${Math.round((plan.completed / plan.total) * 100)}%`;
  const labels = { pending: "待处理", in_progress: "进行中", completed: "已完成", blocked: "阻塞" };
  ui.taskPlanItems.replaceChildren(...plan.items.map((item) => {
    const row = element("div", `task-plan-item ${item.status}`);
    row.append(element("i", "task-plan-marker", item.status === "completed" ? "✓" : ""));
    const copy = element("div", "task-plan-copy");
    copy.append(element("strong", "", item.title));
    if (item.blocker) copy.append(element("small", "", item.blocker));
    row.append(copy, element("span", "task-plan-status", labels[item.status] || item.status));
    return row;
  }));
}

function renderConversation() {
  const task = currentTask();
  if (!task) {
    ui.title.textContent = "新对话"; ui.status.textContent = "就绪";
    ui.workspace.textContent = "尚未选择工作目录"; ui.empty.hidden = false;
    ui.transcript.replaceChildren(); ui.send.disabled = false; ui.stop.hidden = true;
    transcriptTaskId = null; renderedEntryKeys = []; streamingMessage = null; lastStreamingText = "";
    ui.progress.hidden = true;
    ui.taskPlan.hidden = true;
    ui.workspace.disabled = false; ui.composerWorkspace.disabled = false;
    ui.permissionMode.value = state.settings.approval_mode || "risk";
    ui.permissionMode.disabled = true;
    ui.worktreeButton.hidden = true;
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
  ui.worktreeButton.hidden = !task.project_id;
  ui.worktreeButton.disabled = task.running || task.workspace_changing || Boolean(task.worktree);
  ui.worktreeButton.classList.toggle("active", Boolean(task.worktree));
  ui.worktreeButton.lastChild.textContent = task.worktree ? ` ${task.worktree.branch}` : " 隔离";
  ui.worktreeButton.title = task.worktree
    ? `已隔离到 ${task.worktree.workspace}，主工作区不会自动合并`
    : "为当前对话创建独立 Git worktree";
  ui.stop.hidden = !task.running;
  renderTaskPlan(task);
  renderProgress(task);
  const streaming = task.streaming_content || "";
  ui.empty.hidden = task.entries.length > 0 || Boolean(streaming);
  renderTranscript(task, streaming);
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
    applyState(await api("/api/state"));
  } catch (error) {
    if (!silent) toast(error.message);
    ui.connection.textContent = "本机服务连接失败";
  }
}

function applyState(nextState) {
  if (Number.isFinite(state.revision) && Number.isFinite(nextState.revision) && nextState.revision < state.revision) return;
  state = nextState;
  if (!state.current_id && state.tasks.length) state.current_id = state.tasks[0].id;
  render();
}

function connectStateEvents() {
  if (!("EventSource" in window) || stateEvents) return;
  stateEvents = new EventSource("/api/events");
  stateEvents.addEventListener("state", (event) => {
    try { applyState(JSON.parse(event.data)); }
    catch (_error) { /* A later event or polling refresh will recover state. */ }
  });
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

async function openDiff(path, entryId = "") {
  const task = currentTask();
  if (!task) return;
  try {
    const scope = entryId ? `${encodeURIComponent(entryId)}/` : "";
    const data = await api(`/api/conversations/${task.id}/changes/${scope}${encodeURIComponent(path)}`);
    const change = data.change;
    ui.diffPath.textContent = change.path; ui.diffPath.title = change.path;
    ui.diffCounts.replaceChildren(element("b", "", `+${change.added}`), element("i", "", `−${change.deleted}`));
    ui.diffWarning.hidden = !change.warning; ui.diffWarning.textContent = change.warning || "";
    ui.diffContent.replaceChildren(...change.rows.map(diffRow));
    ui.diffView.hidden = false; ui.operationsView.hidden = true;
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

function shortCommit(value) { return value ? String(value).slice(0, 8) : "未记录"; }

function displayTime(value) {
  if (!value) return "时间未知";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

function fillTaskPrompt(text) {
  ui.input.value = text;
  closeDiff();
  ui.input.focus();
}

function statusTone(value) {
  const normalized = String(value || "").toLowerCase();
  if (["success", "healthy", "running", "released", "rolled_back"].includes(normalized)) return "good";
  if (["failed", "failure", "unhealthy", "dead", "exited"].includes(normalized)) return "bad";
  return "pending";
}

function releaseNode(release, activeVersion, environment) {
  const row = element("article", `release-entry ${release.version === activeVersion ? "active" : ""}`);
  const marker = element("span", `release-marker ${statusTone(release.status)}`);
  const body = element("div", "release-entry-body");
  const top = element("div", "release-entry-top");
  top.append(element("strong", "release-version", release.version || "未命名版本"));
  if (release.version === activeVersion) top.append(element("span", "active-badge", "当前运行"));
  top.append(element("time", "release-time", displayTime(release.created_at)));
  const meta = element("div", "release-meta");
  meta.append(
    element("span", "", release.git && release.git.branch ? release.git.branch : "无分支"),
    element("code", "", shortCommit(release.git && release.git.commit)),
    element("span", `release-health ${release.healthy ? "good" : "bad"}`, release.healthy ? "健康" : "未通过"),
  );
  const actionsEvidence = release.github_actions && release.github_actions.status;
  if (actionsEvidence) meta.append(element("span", `ci-chip ${statusTone(actionsEvidence.overall)}`, `CI ${actionsEvidence.overall}`));
  body.append(top, meta);
  if (release.images && release.images.length) {
    const images = element("div", "release-images");
    release.images.forEach((image) => images.append(element("code", "", `${image.reference} · ${image.id}`)));
    body.append(images);
  }
  if (release.version && release.version !== activeVersion && release.status === "released") {
    const rollback = element("button", "rollback-prompt", "生成回滚计划");
    rollback.type = "button";
    rollback.addEventListener("click", () => fillTaskPrompt(`查询 ${environment} 的发布历史，并为版本 ${release.version} 生成回滚计划。先展示影响，不要直接执行回滚。`));
    body.append(rollback);
  }
  row.append(marker, body);
  return row;
}

function environmentNode(environment) {
  const card = element("section", "environment-card");
  const head = element("header", "environment-card-head");
  const identity = element("div", "environment-identity");
  identity.append(element("span", "environment-context", environment.docker_context || "current"), element("h3", "", environment.name));
  const live = environment.operation && environment.operation.busy;
  head.append(identity, element("span", `environment-lock ${live ? "busy" : "free"}`, live ? "操作进行中" : "环境空闲"));
  card.append(head);

  if (environment.error) {
    const error = element("div", "operations-error");
    error.append(element("strong", "", environment.error.code), element("p", "", environment.error.message));
    const diagnose = element("button", "quiet-button compact", "让 Agent 诊断");
    diagnose.type = "button";
    diagnose.addEventListener("click", () => fillTaskPrompt(`诊断 ${environment.name} 环境：${environment.error.message}`));
    error.append(diagnose); card.append(error); return card;
  }

  const summary = element("div", "environment-summary");
  const version = element("div", "environment-version");
  version.append(element("small", "", "ACTIVE VERSION"), element("strong", "", environment.active_version || "尚未发布"));
  const services = element("div", "service-strip");
  if (!environment.services.length) services.append(element("span", "service-empty", "没有运行中的服务"));
  environment.services.forEach((service) => {
    const chip = element("span", `service-chip ${statusTone(service.health === "healthy" ? "healthy" : service.state)}`);
    chip.append(element("i", ""), document.createTextNode(`${service.service} · ${service.health || service.state}`));
    services.append(chip);
  });
  summary.append(version, services); card.append(summary);

  const rail = element("div", "release-rail");
  if (!environment.releases.length) {
    const empty = element("div", "release-empty");
    empty.append(element("strong", "", "还没有版本记录"), element("p", "", "使用 compose_release 后，版本、Commit、CI 和镜像证据会出现在这里。"));
    rail.append(empty);
  } else {
    environment.releases.forEach((release) => rail.append(releaseNode(release, environment.active_version, environment.name)));
  }
  card.append(rail);
  return card;
}

function renderOperations(overview) {
  ui.operationsContent.replaceChildren();
  const manifest = element("section", "operations-manifest");
  const copy = element("div", "operations-manifest-copy");
  copy.append(element("span", "operations-eyebrow", overview.compose_file ? "COMPOSE CONNECTED" : "COMPOSE NOT FOUND"));
  copy.append(element("strong", "", overview.compose_file || "选择包含 Compose 配置的项目"));
  copy.append(element("p", "", overview.workspace));
  const gate = element("div", "operations-gate");
  gate.append(element("small", "", "RELEASE GATE"));
  const gateParts = [];
  if (overview.release_policy.require_clean_worktree) gateParts.push("Git clean");
  if (overview.release_policy.checks.length) gateParts.push(`${overview.release_policy.checks.length} checks`);
  if (overview.github_actions.require_success) gateParts.push("CI required");
  gate.append(element("strong", "", gateParts.join(" · ") || "基础门禁"));
  manifest.append(copy, gate); ui.operationsContent.append(manifest);
  overview.environments.forEach((environment) => ui.operationsContent.append(environmentNode(environment)));
}

async function openOperations() {
  const task = currentTask();
  if (!task || !task.workspace) { toast("请先为当前对话选择工作目录"); return; }
  ui.diffView.hidden = true; ui.operationsView.hidden = false;
  ui.shell.classList.add("diff-open"); ui.diffPanel.setAttribute("aria-hidden", "false");
  ui.operationsContent.replaceChildren(element("div", "operations-loading", "正在读取环境、容器和发布证据…"));
  ui.refreshOperations.disabled = true;
  try {
    const data = await api(`/api/conversations/${task.id}/devops-overview`);
    renderOperations(data.overview);
  } catch (error) {
    const failure = element("div", "operations-error prominent");
    failure.append(element("strong", "", "发布台无法加载"), element("p", "", error.message));
    ui.operationsContent.replaceChildren(failure);
  } finally { ui.refreshOperations.disabled = false; }
}

function openWorktreeDialog() {
  const task = currentTask();
  if (!task || !task.workspace) { toast("请先为当前对话选择 Git 工作目录"); return; }
  if (task.worktree) { toast(`当前对话已隔离到 ${task.worktree.branch}`); return; }
  ui.worktreeSource.textContent = task.source_workspace || task.workspace;
  ui.worktreeBranchPreview.textContent = `coding-agent/task-${task.id.slice(0, 12).toLowerCase()}`;
  ui.worktreeDialog.showModal();
}

async function createTaskWorktree(event) {
  event.preventDefault();
  const task = currentTask();
  if (!task) return;
  ui.confirmWorktree.disabled = true;
  ui.confirmWorktree.textContent = "正在创建…";
  try {
    await api(`/api/conversations/${task.id}/worktree`, { method: "POST", body: {} });
    ui.worktreeDialog.close();
    await refresh(false);
    toast("隔离工作区已创建，后续操作不会改动主工作区");
  } catch (error) { toast(error.message); }
  finally {
    ui.confirmWorktree.disabled = false;
    ui.confirmWorktree.textContent = "创建隔离工作区";
  }
}
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
document.querySelector("#open-operations").addEventListener("click", openOperations);
document.querySelector("#create-worktree").addEventListener("click", openWorktreeDialog);
document.querySelector("#close-operations").addEventListener("click", closeDiff);
document.querySelector("#refresh-operations").addEventListener("click", openOperations);
document.querySelector("#open-sidebar").addEventListener("click", openSidebar);
document.querySelector("#close-sidebar").addEventListener("click", closeSidebar);
document.querySelector("#sidebar-scrim").addEventListener("click", closeSidebar);
document.querySelector("#stop-task").addEventListener("click", stopTask);
document.querySelector("#cancel-operation").addEventListener("click", stopTask);
document.querySelector("#approve-command").addEventListener("click", () => resolveApproval(true));
document.querySelector("#deny-command").addEventListener("click", () => resolveApproval(false));
ui.workspaceForm.addEventListener("submit", submitWorkspace);
ui.worktreeForm.addEventListener("submit", createTaskWorktree);
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
}).finally(connectStateEvents);
setInterval(() => refresh(true), 5000);
