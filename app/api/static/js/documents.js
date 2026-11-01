import {api, mutate, jsonMutation, pending} from './api.js?v=workspace-4-2';
import {state, checkpoint, selectionDirty, emit} from './state.js?v=workspace-4-2';
import {$, node, action, empty, notify, handleAction, choose} from './ui.js?v=workspace-4-2';

let librarySequence = 0, previewSequence = 0, selectionSequence = 0;
const projectPath = id => `/api/projects/${id}/documents`;
const retrieverNames = {bm25:'关键词检索', vector:'语义检索', hybrid:'关键词＋语义', hybrid_rerank:'混合检索＋重排'};

export async function loadSelection() {
  if (!state.task || state.task.task_mode !== 'knowledge') return;
  const current = checkpoint(), seq = ++selectionSequence, taskId = state.taskId, projectId = state.projectId;
  const [docs, settings] = await Promise.all([api(projectPath(projectId)), api(`/api/tasks/${taskId}/knowledge-settings`)]);
  if (!current() || seq !== selectionSequence) return;
  let edit = state.selections.get(taskId);
  if (!edit || !selectionDirty(taskId)) edit = {base:settings, ids:new Set(settings.document_ids), retriever:settings.retriever};
  edit.docs = docs; state.selections.set(taskId, edit); renderSelection();
}
export function renderSelection() {
  const edit = state.selections.get(state.taskId); if (!edit) return;
  const root = $('#task-documents'); root.replaceChildren();
  const available = new Map(edit.docs.map(d => [d.document_id,d]));
  const ids = [...available.keys(), ...[...edit.ids].filter(id => !available.has(id))];
  if (!ids.length) empty(root, '项目还没有资料，请先上传，再选择并应用到任务。');
  for (const id of ids) {
    const doc = available.get(id), checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.checked = edit.ids.has(id);
    checkbox.onchange = () => { if (checkbox.checked) edit.ids.add(id); else edit.ids.delete(id); selectionChanged(); };
    const label = node('label', undefined, 'check'); label.append(checkbox, node('span', doc ? doc.name + ' · 版本 ' + doc.revision : '所选资料已不可用，请取消选择'));
    root.append(label);
  }
  $('#retriever').value = edit.retriever;
  for (const option of $('#retriever').options) option.disabled = option.value !== 'bm25' && (state.task.run_environment === 'offline' || !state.config.live_enabled);
  selectionChanged();
}
export function selectionChanged() {
  const edit = state.selections.get(state.taskId);
  $('#selection-status').textContent = selectionDirty() ? ' · 有未应用修改' : ' · 已应用';
  const unresolved=!!pending('selection:' + state.taskId);
  $('#apply-selection').textContent=unresolved ? '核对上次应用' : '应用到任务';
  $('#apply-selection').disabled = !edit || edit.ids.size > 20 || (!selectionDirty() && !unresolved);
  emit('task:status');
}
export async function applySelection() {
  const taskId = state.taskId, edit = state.selections.get(taskId); if (!edit) return;
  const current = checkpoint();
  const unresolved=pending('selection:' + taskId), dirty=selectionDirty(taskId);
  const payload = unresolved?.payload || {expected_revision:edit.base.revision, document_ids:[...edit.ids].sort(), retriever:edit.retriever};
  let settings = await jsonMutation('selection:' + taskId, `/api/tasks/${taskId}/knowledge-settings`, payload, 'PUT');
  if (unresolved) {
    settings=await api(`/api/tasks/${taskId}/knowledge-settings`);
    if (!dirty) { edit.ids=new Set(settings.document_ids); edit.retriever=settings.retriever; }
  }
  // Keep edits made while this request was pending; only the applied base advances.
  edit.base = settings;
  if (current()) { renderSelection(); emit('task:refresh'); notify('资料选择和检索方式已应用。'); }
}
export async function reloadSelection() {
  const taskId = state.taskId, current = checkpoint(), seq = ++selectionSequence;
  const settings = await api(`/api/tasks/${taskId}/knowledge-settings`);
  if (!current() || seq !== selectionSequence) return;
  const edit = state.selections.get(taskId);
  if (selectionDirty(taskId)) {
    const selected = (edit.docs || []).filter(d => settings.document_ids.includes(d.document_id)).map(d => d.name).join('、') || '无';
    const answer = await choose('核对已保存设置', '当前已保存：' + selected + '；' + retrieverNames[settings.retriever] + '。本地修改尚未应用。', [['keep','保留本地修改继续核对'],['discard','使用已保存设置'],['cancel','继续编辑']]);
    if (!current() || answer === 'cancel') return;
    edit.base = settings;
    if (answer === 'discard') { edit.ids = new Set(settings.document_ids); edit.retriever = settings.retriever; }
  } else state.selections.delete(taskId);
  await loadSelection();
}
export async function leaveSelection() {
  if (!selectionDirty()) return true;
  const edit = state.selections.get(state.taskId);
  const result = await choose('资料修改尚未应用', '保留后可返回继续编辑；应用到任务后才会用于发送。', [['keep','保留修改并离开'],['discard','放弃修改并离开'],['cancel','继续编辑']]);
  if (result === 'discard') { edit.ids = new Set(edit.base.document_ids); edit.retriever = edit.base.retriever; }
  return result !== 'cancel';
}
export async function loadDocuments() {
  const seq = ++librarySequence, current = checkpoint(), projectId = state.projectId;
  ++previewSequence; $('#document-versions').replaceChildren(); $('#document-preview').textContent = '选择资料查看版本与原件。'; $('#preview-title').textContent = '资料预览';
  $('#upload').disabled = !projectId;
  if (!projectId) { empty($('#documents'), '请先选择项目。'); $('#document-count').textContent = ''; return; }
  const docs = await api(projectPath(projectId)); if (!current() || seq !== librarySequence) return;
  const root = $('#documents'); root.replaceChildren(); $('#document-count').textContent = docs.length + ' 份';
  if (!docs.length) empty(root, '还没有资料，先上传一份文件。');
  for (const doc of docs) {
    const row = node('div', undefined, 'list-item'), info = node('div');
    info.append(node('strong', doc.name), node('p', '版本 ' + doc.revision + ' · ' + (doc.current_version.byte_count / 1024).toFixed(1) + ' KB'));
    row.append(info, action('预览与版本', () => previewDocument(projectId,doc)), action('修订', () => pickRevision(projectId,doc))); root.append(row);
  }
}
function pickRevision(projectId, doc) {
  const input = node('input'); input.type = 'file'; input.accept = doc.media_type;
  input.onchange = handleAction(async () => { if (input.files[0]) await publishFile(projectId,input.files[0],doc); }); input.click();
}
export async function publishFile(projectId, file, doc = null) {
  if (file.size > 4 * 1024 * 1024) throw new Error('单份资料最多 4 MiB。');
  const buffer = await file.arrayBuffer();
  const sha = [...new Uint8Array(await crypto.subtle.digest('SHA-256',buffer))].map(b => b.toString(16).padStart(2,'0')).join('');
  const key = `upload:${projectId}:${doc?.document_id || file.name}`;
  const previous=pending(key)?.payload;
  const revision=previous?.sha === sha ? previous.expected_revision : doc?.revision || null;
  const payload = {project_id:projectId, document_id:doc?.document_id || null, name:doc?.name || file.name, sha, expected_revision:revision};
  const current = checkpoint();
  await mutate(key, payload, id => api(doc ? `${projectPath(projectId)}/${doc.document_id}/versions?request_id=${id}&expected_revision=${revision}` : `${projectPath(projectId)}?request_id=${id}&name=${encodeURIComponent(file.name)}`, {method:'POST', headers:{'Content-Type':'application/octet-stream'}, body:buffer}));
  notify('资料已发布。任务选择需点击应用，已接收的运行继续使用原版本。');
  if (current()) { if (state.view === 'knowledge') await loadDocuments(); if (state.task?.task_mode === 'knowledge') await loadSelection(); emit('task:refresh'); }
}
export async function uploadDocument() {
  const input = $('#upload'), projectId = state.projectId, file = input.files[0]; if (!file) return;
  if (!projectId) throw new Error('请先选择项目。');
  try { await publishFile(projectId,file); } finally { input.value = ''; }
}
async function previewDocument(projectId, doc) {
  const seq = ++previewSequence, current = checkpoint();
  const versions = await api(`${projectPath(projectId)}/${doc.document_id}/versions`);
  if (!current() || seq !== previewSequence) return;
  $('#preview-title').textContent = doc.name;
  const chooser = node('select'); chooser.setAttribute('aria-label','资料历史版本');
  for (const version of versions.toReversed()) chooser.append(new Option('版本 ' + version.revision, version.version_id));
  const display = () => {
    if (!current() || seq !== previewSequence) return;
    const version = versions.find(v => v.version_id === chooser.value), preview = version.preview;
    $('#document-preview').textContent = preview.text + (preview.truncated ? '\n\n预览已截短。' : '') + (preview.pages_without_text.length ? '\n\n无文字页面：' + preview.pages_without_text.join('、') : '');
    const link = node('a','查看此版本原件','text-button'); link.href = `${projectPath(projectId)}/${doc.document_id}/versions/${version.version_id}/raw`; link.target = '_blank'; link.rel = 'noopener';
    $('#document-versions').replaceChildren(chooser,link);
  };
  chooser.onchange = display; display();
}
