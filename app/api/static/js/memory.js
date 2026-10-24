import { api, post } from "./api.js";
import { state } from "./state.js";
import { $, badge, empty, handleAction, node, notify } from "./ui.js";
import { memoryScope } from "./workspace.js";

async function revokeMemory(id) {
  await post("/api/memories/" + id + "/revoke?" + memoryScope());
  await loadMemory();
  notify("这条记忆已撤回，历史记录仍保留。");
}

function memoryRow(record) {
  const row = node("div", undefined, "list-item");
  const info = node("div");
  const kind = record.key === "project.model" ? "项目模型决定" : "回答偏好";
  info.append(
    node("strong", record.value),
    node("p", kind + " · 来源：" + record.source.session_id + "\n原句：" + record.source.quote),
  );
  row.append(info, badge(record.status));

  if (record.status === "active") {
    const button = node("button", "撤回", "text-button");
    button.disabled = !!state.active;
    button.onclick = handleAction(() => revokeMemory(record.memory_id));
    row.append(button);
  }
  return row;
}

export async function loadMemory() {
  const history = $("#memory-history").checked;
  const records = await api("/api/memories?" + memoryScope() + "&history=" + history);
  const container = $("#memory-list");
  container.replaceChildren();
  if (!records.length) {
    empty(container, "当前范围内还没有项目记忆。可在工作对话中启用记忆并明确说明项目决定。");
  }
  for (const record of records) container.append(memoryRow(record));
}
