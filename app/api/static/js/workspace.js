import { api } from "./api.js";
import { state } from "./state.js";
import { $ } from "./ui.js";

const repositoryStorageKey = "projectpilot.repository";

function repositoryScope(engine) {
  const repository = $("#repository-select").value;
  return engine === "repository" && repository
    ? "&repository=" + encodeURIComponent(repository)
    : "";
}

// 会话和记忆都按任务类型、运行模式及仓库隔离，避免切换后串用数据。
export function sessionScope() {
  const engine = $("#engine").value;
  return "engine=" + encodeURIComponent(engine)
    + "&mode=" + $("#mode").value + repositoryScope(engine);
}

export function memoryScope() {
  const engine = $("#memory-engine").value;
  return "engine=" + engine
    + "&mode=" + $("#mode").value + repositoryScope(engine);
}

export function saveRepositorySelection() {
  localStorage.setItem(repositoryStorageKey, $("#repository-select").value);
}

export function updateSelectionHint() {
  const engine = $("#engine").value;
  $("#selected-documents").textContent = [...state.documents].join("、") || "尚未选择";
  $("#knowledge-selection").hidden = engine !== "knowledge";
  const offline = $("#mode").value !== "live";
  for (const option of $("#retriever").options) {
    option.disabled = offline && option.value !== "bm25";
  }
  if (offline) $("#retriever").value = "bm25";
  $("#repository-settings").hidden = engine !== "repository";

  const selected = state.repositories.find(
    (repository) => repository.id === $("#repository-select").value,
  );
  const imported = engine === "repository" && selected?.id;
  if (state.config) {
    $("#workspace-name").textContent = imported ? selected.name : state.config.workspace_name;
    $("#workspace-path").textContent = imported ? selected.url : state.config.workspace;
  }
}

export async function loadRepositories(preferred) {
  state.repositories = await api("/api/repositories");
  const select = $("#repository-select");
  const current = preferred
    ?? (select.value || localStorage.getItem(repositoryStorageKey) || "");

  select.replaceChildren();
  for (const repository of state.repositories) {
    const title = repository.id ? repository.name : "启动时项目 · " + repository.name;
    select.append(new Option(title, repository.id));
  }
  select.value = state.repositories.some((repository) => repository.id === current)
    ? current
    : "";
  saveRepositorySelection();
  updateSelectionHint();
}
