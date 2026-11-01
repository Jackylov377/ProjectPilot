import {api, jsonMutation, pending} from './api.js?v=workspace-4-2';
import {state, saved, save, draftKey, checkpoint, selectionDirty, modeTitle, emit} from './state.js?v=workspace-4-2';
import {$, node, empty, action, statusTitles} from './ui.js?v=workspace-4-2';
import {renderUsage} from './usage.js?v=workspace-4-2';

let historySequence = 0;
const policies = {window:'保留最近对话', full:'保留完整历史', summary:'摘要与任务状态'};
const blocked = {documents_required:'请先选择至少一份项目资料并应用到任务。', document_unavailable:'所选资料已缺失或损坏，请核对原件。', source_binding_required:'项目尚未绑定可用仓库，请核对项目来源。', source_unavailable:'仓库来源不可用，请核对项目与来源。', source_mismatch:'仓库来源已改变，请核对项目绑定。', live_unavailable:'真实模型尚未启用，任务环境不能切换。', retriever_requires_live:'所选检索方式需要真实环境。', retriever_unavailable:'检索服务配置尚未就绪，请配置服务或明确改用关键词检索。', memory_unavailable:'项目记忆不可用，请检查存储后重试。'};
export function renderEnvironment() {
  const task=state.task;
  $('#mode').value=task?.run_environment || (task ? saved('environment.' + task.task_id,saved('preferred-environment','')) : saved('preferred-environment',''));
  $('#mode').disabled=!!task?.run_environment;
  $('#mode option[value="live"]').disabled=!state.config.live_enabled;
  $('#environment-status').textContent=task?.run_environment ? '当前任务已固定' : task ? '首次发送后固定' : '';
  $('#mode').title=task?.run_environment ? '当前任务已固定连接方式，需要不同环境时请新建任务。' : '本地连接使用预设演示响应；真实模型使用已配置的模型服务。';
}
export function renderTask(restore = true) {
  const task = state.task; if (!task) return;
  const mode = task.task_mode === 'repository' ? '仓库分析报告' : '知识资料问答';
  $('#task-subtitle').textContent = `${mode} · 项目记忆${task.memory_enabled ? '已启用' : '已关闭'} · ${policies[task.context_policy]}`;
  const dl = $('#fixed-configuration'); dl.replaceChildren();
  for (const [label,value] of [['任务模式',mode],['项目记忆',task.memory_enabled ? '启用：读取与记录' : '关闭'],['长对话策略',policies[task.context_policy]],['运行环境',modeTitle(task.run_environment)]]) dl.append(node('dt',label),node('dd',value));
  $('#task-materials').hidden = task.task_mode !== 'knowledge';
  renderEnvironment();
  if (restore) { $('#prompt').value = saved(draftKey(task),''); empty($('#messages'),'正在读取本任务历史…'); }
  updateSendState();
}
export function updateSendState() {
  const task = state.task; if (!task) return;
  let reason = '';
  if (state.active) reason = '请等待当前运行结束；可以继续编辑草稿和整理资料。';
  else if (pending('send:' + task.task_id)) reason = '上次发送结果待核对，请使用原请求核对，避免重复执行。';
  else if (pending('selection:' + task.task_id)) reason = '上次资料应用结果待核对，请在任务资料中核对上次应用。';
  else if (selectionDirty()) reason = '资料选择尚未应用，请先点击“应用到任务”。';
  else if (task.task_mode === 'knowledge' && state.selections.get(task.task_id)?.base.retriever !== 'bm25' && $('#mode').value === 'offline') reason = '高级检索需要真实环境，请明确改用关键词检索或选择真实环境。';
  else if (!$('#mode').value) reason = '请在右上角选择本地连接或真实模型。';
  else if (!task.can_run) reason = blocked[task.blocked_reason] || '当前任务暂不可运行，请核对项目来源和任务资料。';
  $('#send-hint').textContent = reason || '';
  $('#send').disabled = !!reason;
  $('#check-send').hidden = !pending('send:' + task.task_id);
  const owns = state.activeJob?.request?.task_id === task.task_id;
  $('#job-status').textContent = owns ? '当前任务运行中' : '就绪';
}
export function saveDraft() { if (state.task) save(draftKey(state.task),$('#prompt').value); }
export function isCurrentConversation(job) { return state.view === 'chat' && job.kind === 'agent' && job.request.task_id === state.taskId && job.request.project_id === state.projectId; }
export function addMessage(role,text,metadata = {},environment = state.task?.run_environment) {
  const article = node('article',undefined,'message ' + role); article.append(node('span',role === 'user' ? '你' : 'ProjectPilot','message-label'),node('div',text,'message-body'));
  if (metadata.run_id) article.dataset.runId = metadata.run_id;
  const interrupted = ['failed','cancelled','interrupted'].includes(metadata.turn_status);
  if (interrupted) { const outcome = node('div',undefined,'turn-outcome'); outcome.append(node('strong',statusTitles[metadata.turn_status])); if (metadata.error) outcome.append(node('p',metadata.error)); if (metadata.error_hint) outcome.append(node('p',metadata.error_hint)); article.append(outcome); }
  if (role === 'assistant' || interrupted) article.append(renderUsage(metadata.usage,{mode:metadata.usage?.mode || environment,interrupted}));
  if (metadata.document_manifest?.length) {
    const detail = node('details'); detail.append(node('summary','本次资料版本（可用清单）'));
    for (const doc of metadata.document_manifest) { const link = node('a',doc.name + ' · 版本 ' + doc.revision,'text-button'); link.href = `/api/projects/${state.projectId}/documents/${doc.document_id}/versions/${doc.version_id}/raw`; link.target='_blank'; link.rel='noopener'; detail.append(link); }
    article.append(detail);
  }
  $('#messages').append(article);
}
export async function loadHistory() {
  if (!state.task || state.view !== 'chat') return;
  const task = state.task, seq = ++historySequence, current = checkpoint();
  const history = await api(`/api/tasks/${task.task_id}/history`);
  if (!current() || seq !== historySequence) return;
  const root = $('#messages'); root.replaceChildren();
  if (!history.length) empty(root,'还没有消息。任务已保存，可以从一个具体问题开始。');
  for (const row of history) addMessage(row.role,row.content,row,task.run_environment);
  root.scrollTop = root.scrollHeight;
}
async function submitPayload(task, payload) {
  const job = await jsonMutation('send:' + task.task_id, `/api/tasks/${task.task_id}/runs`,payload);
  const submitted=saved('submitted-draft.' + task.task_id,payload.prompt);
  if (saved(draftKey(task),'') === submitted) save(draftKey(task),'');
  if (state.taskId === task.task_id && $('#prompt').value === submitted) { $('#prompt').value=''; save(draftKey(task),''); }
  if (isCurrentConversation(job) && !$('#messages').querySelector(`[data-run-id="${job.id}"]`)) { $('#messages .empty-copy')?.remove(); addMessage('user',payload.prompt,{run_id:job.id}); }
  emit('task:refresh'); emit('job:watch',{job});
  return job;
}
let submitting = false;
export async function submitChat(event) {
  event.preventDefault(); if (submitting || !state.task || state.active) return;
  if (selectionDirty()) throw new Error('请先应用资料选择。');
  const task = state.task, prompt = $('#prompt').value.trim(); if (!prompt) return;
  const payload = {prompt}; if (!task.run_environment) { if (!$('#mode').value) throw new Error('请在右上角选择连接方式。'); payload.environment = $('#mode').value; }
  saveDraft(); if (!pending('send:' + task.task_id)) save('submitted-draft.' + task.task_id,$('#prompt').value); submitting = true; $('#send').disabled = true;
  try { await submitPayload(task,payload); } finally { submitting=false; updateSendState(); }
}
export async function checkSend() {
  const task = state.task, intent = pending('send:' + task.task_id); if (!intent || submitting) return;
  submitting=true;
  try { await submitPayload(task,intent.payload); } finally { submitting=false; updateSendState(); }
}
