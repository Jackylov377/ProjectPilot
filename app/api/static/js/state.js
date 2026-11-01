// Identity and request generations are shared; server records remain authoritative.
export const state = {
  view: 'chat', epoch: 0, config: null, projects: [], tasks: new Map(),
  projectId: null, taskId: null, project: null, task: null,
  active: null, activeJob: null, events: null, selections: new Map(),
};
export const viewTitles = {chat:'工作对话', knowledge:'项目资料', memory:'项目记忆',
  runs:'运行记录', evaluations:'评测中心', legacy:'改造前数据', backlog:'待办与缺陷'};
export const requestId = () => crypto.randomUUID().replaceAll('-', '');
export function emit(name, detail = {}) { window.dispatchEvent(new CustomEvent(name, {detail})); }
export function checkpoint() {
  const epoch = state.epoch;
  return () => epoch === state.epoch;
}
function storageKey(key) { return 'projectpilot.ui.' + state.config?.storage_namespace + '.' + key; }
export function saved(key, fallback = null) {
  try { return JSON.parse(localStorage.getItem(storageKey(key))) ?? fallback; } catch { return fallback; }
}
let warned = false;
export function save(key, value) {
  try {
    if (value === undefined) localStorage.removeItem(storageKey(key));
    else localStorage.setItem(storageKey(key), JSON.stringify(value));
  } catch {
    if (!warned) { warned = true; emit('ui:notice', {message:'浏览器无法保存本地草稿，请在离开前保留输入。', error:true}); }
  }
}
export const draftKey = task => `draft.${state.config.workspace_id}.${task.project_id}.${task.task_id}`;
export function selectionDirty(taskId = state.taskId) {
  const edit = state.selections.get(taskId);
  return !!edit && (edit.retriever !== edit.base.retriever || JSON.stringify([...edit.ids].sort()) !== JSON.stringify([...edit.base.document_ids].sort()));
}
export const modeTitle = mode => mode === 'live' ? '真实模型' : mode === 'offline' ? '本地连接（演示）' : '尚未选择';
