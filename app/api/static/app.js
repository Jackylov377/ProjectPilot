// 阅读入口：先看 initialize 与 bindEvents，再按页面进入 js/ 中的对应模块。
// 请求在 api.js，共用状态在 state.js；任务进度与最终结果的交接在 jobs.js。
import { api } from "./js/api.js";
import { loadHistory, loadSessions, submitChat } from "./js/chat.js";
import { loadDocuments, uploadDocument } from "./js/documents.js";
import { loadEvaluations, submitEvaluation } from "./js/evaluations.js";
import { cancelActiveJob, importRepository, restoreRunningJob, watchJob } from "./js/jobs.js";
import { loadMemory } from "./js/memory.js";
import { loadRuns } from "./js/runs.js";
import { state, viewTitles } from "./js/state.js";
import { $, $$, handleAction, listen, node, notify } from "./js/ui.js";
import { loadRepositories, saveRepositorySelection, updateSelectionHint } from "./js/workspace.js";

async function loadBacklog() {
  const data = await api("/api/backlog");
  $("#backlog-count").textContent = data.items.length;
  $("#backlog-policy").textContent = data.policy;
  const container = $("#backlog-list");
  container.replaceChildren();
  for (const item of data.items) {
    const card = node("article", undefined, "backlog-card");
    card.append(
      node("span", item.phase, "badge"),
      node("h3", item.title),
      node("p", item.detail),
      node("small", item.status === "deferred" ? "已记录 · 暂缓处理" : "后续完善"),
    );
    container.append(card);
  }
}

const viewLoaders = {
  knowledge: loadDocuments,
  runs: loadRuns,
  evaluations: loadEvaluations,
  memory: loadMemory,
  backlog: loadBacklog,
};

async function navigate(view) {
  if (!viewTitles[view]) view = "chat";
  state.view = view;
  location.hash = view;
  $$(".panel").forEach((panel) => {
    panel.hidden = panel.id !== "panel-" + view;
  });
  $$("nav button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  $("#view-title").textContent = viewTitles[view];
  document.title = "ProjectPilot · " + viewTitles[view];
  if (viewLoaders[view]) await viewLoaders[view]();
}

async function startNewSession() {
  $("#session").value = "chat-" + Date.now().toString(36);
  await loadSessions();
  $("#prompt").focus();
}

async function changeMode() {
  $("#mode-note").textContent = $("#mode").value === "offline"
    ? "本地演示使用预设响应，仅验证操作流程。"
    : "将使用已配置的模型服务处理本次问题与资料。";
  await loadSessions();
  if (state.view === "memory") await loadMemory();
}

async function askSelectedDocuments() {
  if (!state.documents.size) throw new Error("请先勾选至少一份资料。");
  $("#engine").value = "knowledge";
  updateSelectionHint();
  await loadSessions();
  await navigate("chat");
  $("#prompt").focus();
}

function handlePageClick(event) {
  const navigation = event.target.closest("[data-view],[data-goto]");
  if (navigation) {
    handleAction(() => navigate(navigation.dataset.view || navigation.dataset.goto))();
  }
  const suggestion = event.target.closest("[data-prompt]");
  if (suggestion) {
    $("#prompt").value = suggestion.dataset.prompt;
    if (suggestion.dataset.memory) $("#use-memory").checked = true;
    $("#prompt").focus();
  }
}

function bindEvents() {
  listen("#chat-form", "submit", async (event) => {
    const job = await submitChat(event);
    if (job) watchJob(job);
  });
  listen("#cancel-job", "click", cancelActiveJob);
  listen("#new-session", "click", startNewSession);
  listen("#session-list", "change", async () => {
    $("#session").value = $("#session-list").value;
    await loadHistory();
  });
  listen("#session", "change", loadSessions);
  listen("#engine", "change", async () => {
    updateSelectionHint();
    await loadSessions();
  });
  listen("#repository-select", "change", async () => {
    saveRepositorySelection();
    updateSelectionHint();
    await loadSessions();
  });
  listen("#import-repository", "click", importRepository);
  listen("#mode", "change", changeMode);
  listen("#upload", "change", uploadDocument);
  listen("#ask-documents", "click", askSelectedDocuments);
  listen("#refresh-runs", "click", loadRuns);
  listen("#refresh-evaluations", "click", loadEvaluations);
  listen("#refresh-memory", "click", loadMemory);
  listen("#memory-engine", "change", loadMemory);
  listen("#memory-history", "change", loadMemory);
  listen("#select-all-cases", "click", () => {
    for (const option of $("#eval-cases").options) option.selected = true;
  });
  listen("#eval-form", "submit", async (event) => {
    const job = await submitEvaluation(event);
    watchJob(job);
    notify("评测已开始，完成后会保留逐题结果。");
  });
  document.addEventListener("click", handlePageClick);
  window.addEventListener("hashchange", handleAction(async () => {
    const view = location.hash.slice(1);
    if (view !== state.view) await navigate(view);
  }));
}

async function initialize() {
  try {
    state.config = await api("/api/config");
    $("#workspace-name").textContent = state.config.workspace_name;
    $("#workspace-path").textContent = state.config.workspace;
    $("#connection").textContent = "本地已连接";
    if (state.config.live_enabled) {
      const live = $("#mode option[value=live]");
      live.disabled = false;
      live.textContent = "真实模型";
    }
    await loadRepositories();
    await loadSessions();
    await loadBacklog();
    await navigate(location.hash.slice(1) || "chat");
    await restoreRunningJob();
  } catch (error) {
    $("#connection").textContent = "连接失败";
    notify(error.message, true);
  }
}

bindEvents();
initialize();
