import { api, post } from "./api.js";
import { state } from "./state.js";
import { $, node } from "./ui.js";
import { sessionScope } from "./workspace.js";

const originalEmptyState = $("#messages").firstElementChild.cloneNode(true);

export function addMessage(role, text) {
  const messages = $("#messages");
  if (messages.querySelector(".empty-state")) messages.replaceChildren();

  const message = node("article", undefined, "message " + role);
  message.append(
    node("span", role === "user" ? "你" : "ProjectPilot", "message-label"),
    node("div", text, "message-body"),
  );
  messages.append(message);
  messages.scrollTop = messages.scrollHeight;
}

export async function loadHistory() {
  const session = $("#session").value;
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(session)) return;

  const history = await api("/api/sessions/" + session + "?" + sessionScope());
  const messages = $("#messages");
  messages.replaceChildren();
  if (!history.length) {
    messages.append(originalEmptyState.cloneNode(true));
    return;
  }
  for (const message of history) addMessage(message.role, message.content);
}

export async function loadSessions() {
  const sessions = await api("/api/sessions?" + sessionScope());
  const current = $("#session").value;
  const select = $("#session-list");
  select.replaceChildren();
  const names = new Set([current, ...sessions.map((item) => item.session)]);
  for (const name of names) {
    select.append(new Option(name, name, false, name === current));
  }
  await loadHistory();
}

export function isCurrentConversation(job) {
  return job.kind === "agent"
    && job.request.session === $("#session").value
    && job.request.engine === $("#engine").value
    && job.request.mode === $("#mode").value;
}

// 提交和进度订阅分开：这里保存请求，jobs.js 负责跟踪返回的任务。
export async function submitChat(event) {
  event.preventDefault();
  if (state.active) return null;
  const prompt = $("#prompt").value.trim();
  if (!prompt) return null;

  const engine = $("#engine").value;
  const job = await post("/api/chat", {
    prompt,
    session: $("#session").value,
    engine,
    mode: $("#mode").value,
    context_policy: $("#context-policy").value,
    memory: $("#use-memory").checked,
    documents: [...state.documents],
    repository: engine === "repository" ? $("#repository-select").value || null : null,
  });
  addMessage("user", prompt);
  $("#prompt").value = "";
  return job;
}
