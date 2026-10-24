import { api } from "./api.js";
import { state } from "./state.js";
import { $, empty, handleAction, node, notify } from "./ui.js";
import { updateSelectionHint } from "./workspace.js";

async function previewDocument(name) {
  const data = await api("/api/documents/" + encodeURIComponent(name));
  const truncated = data.truncated ? "\n\n[预览仅显示前半部分]" : "";
  const missingText = data.pages_without_text.length
    ? "\n\n部分PDF页面没有提取到文字。"
    : "";
  $("#preview-title").textContent = name;
  $("#document-preview").textContent = data.text + truncated + missingText;
}

function documentRow(item) {
  const row = node("div", undefined, "list-item");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = state.documents.has(item.name);
  checkbox.setAttribute("aria-label", "选择 " + item.name);
  checkbox.onchange = () => {
    if (checkbox.checked) state.documents.add(item.name);
    else state.documents.delete(item.name);
    updateSelectionHint();
  };

  const info = node("div");
  info.append(
    node("strong", item.name),
    node("p", (item.bytes / 1024).toFixed(1) + " KB"),
  );
  const preview = node("button", "预览", "text-button");
  preview.onclick = handleAction(() => previewDocument(item.name));
  row.append(checkbox, info, preview);
  return row;
}

export async function loadDocuments() {
  const documents = await api("/api/documents");
  $("#document-count").textContent = documents.length + " 份";
  const container = $("#documents");
  container.replaceChildren();

  // 刷新后只保留仍存在的资料选择，避免请求携带已经失效的文件名。
  const available = new Set(documents.map((item) => item.name));
  state.documents = new Set([...state.documents].filter((name) => available.has(name)));
  if (!documents.length) empty(container, "还没有资料，先导入一份文件。");
  for (const item of documents) container.append(documentRow(item));
  updateSelectionHint();
}

export async function uploadDocument() {
  const input = $("#upload");
  const file = input.files[0];
  if (!file) return;

  await api("/api/documents/" + encodeURIComponent(file.name), {
    method: "PUT",
    headers: { "Content-Type": "application/octet-stream" },
    body: file,
  });
  input.value = "";
  await loadDocuments();
  notify("资料已导入，选中后即可用于问答。");
}
