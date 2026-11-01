import {api, post} from './api.js?v=workspace-4-2';
import {state, emit} from './state.js?v=workspace-4-2';
import {isCurrentConversation, loadHistory, updateSendState} from './chat.js?v=workspace-4-2';
import {$, node, action, notify, progressLabel, setBusy} from './ui.js?v=workspace-4-2';

let watchSequence = 0, recoveryTimer, cachedPreview;
export function renderActive() {
  const job = state.activeJob; $('#active-run').hidden = !job;
  if (!job) return;
  const p = state.projects.find(p => p.project_id === job.request.project_id);
  const t = (state.tasks.get(job.request.project_id) || []).find(t => t.task_id === job.request.task_id);
  $('#active-label').textContent = job.kind === 'agent' ? (p?.name || '项目') + ' / ' + (t?.name || '任务') + ' · 运行中' : job.kind === 'evaluation' ? '评测运行中' : '正在获取仓库源码';
  $('#return-run').textContent = job.request.task_id ? '返回任务' : '查看运行';
}
function showAnswerPreview(job, preview) {
  if (!isCurrentConversation(job)) return;
  let article = $('#messages').querySelector('[data-preview]');
  if (article && article.dataset.preview !== job.id) { article.remove(); article=null; }
  if (!preview.text) { article?.remove(); return; }
  if (!article) {
    $('#messages .empty-copy')?.remove();
    article = node('article',undefined,'message assistant preview-message'); article.dataset.preview=job.id;
    article.append(node('span','生成预览 · 以最终结果为准','message-label'),node('div','','message-body')); $('#messages').append(article);
  }
  article.querySelector('.message-body').textContent = preview.text;
  $('#messages').scrollTop = $('#messages').scrollHeight;
}
async function finish(job, sequence) {
  if (sequence !== watchSequence) return;
  state.events?.close(); state.events=null; clearTimeout(recoveryTimer); cachedPreview=null;
  if (state.active === job.id) { state.active=null; state.activeJob=null; }
  $('#active-progress').textContent=''; renderActive(); setBusy(false);
  if (isCurrentConversation(job)) { $('#messages [data-preview]')?.remove(); await loadHistory(); emit('task:refresh'); }
  if (job.kind === 'repository_import') emit('sources:changed');
  emit('run:finished',{job}); emit('workspace:reload');
  if (job.status !== 'completed') notify(job.error || '本次运行未完成，记录已保留。',true);
  else if (isCurrentConversation(job) && job.result?.retrieval_warnings?.length) notify(job.result.retrieval_warnings.join('；'));
}
export async function watchJob(job) {
  if (job.status !== 'running') { if (isCurrentConversation(job)) await loadHistory(); emit('task:refresh'); return; }
  if (state.active === job.id && state.events) return;
  state.events?.close(); clearTimeout(recoveryTimer);
  const sequence = ++watchSequence; state.active=job.id; state.activeJob=job;
  renderActive(); setBusy(true); emit('workspace:render');
  $('#active-progress').textContent='正在准备运行…';
  const stream = new EventSource('/api/jobs/' + job.id + '/events'); state.events=stream;
  const alive = () => sequence === watchSequence && state.active === job.id;
  stream.addEventListener('progress', event => { if (alive()) $('#active-progress').textContent=progressLabel(JSON.parse(event.data)); });
  stream.addEventListener('answer', event => { if (alive()) { cachedPreview={job,preview:JSON.parse(event.data)}; showAnswerPreview(job,cachedPreview.preview); } });
  stream.addEventListener('status', event => { if (alive()) { const current=JSON.parse(event.data); if (current.status !== 'running') finish(current,sequence).catch(e => notify(e.message,true)); } });
  stream.addEventListener('done', event => { if (alive()) finish(JSON.parse(event.data),sequence).catch(e => notify(e.message,true)); });
  stream.onerror = () => {
    if (!alive()) return;
    $('#active-progress').textContent='进度正在重连，执行状态以服务端为准。';
    clearTimeout(recoveryTimer);
    recoveryTimer=setTimeout(async () => {
      if (!alive()) return;
      try { const current=await api('/api/jobs/' + job.id); if (current.status !== 'running') await finish(current,sequence); }
      catch { if (alive()) $('#active-progress').textContent='暂时无法核对运行，请恢复连接后刷新。'; }
    },1500);
  };
  updateSendState();
}
export async function restoreRunningJob() {
  const jobs=await api('/api/jobs');
  const running=jobs.find(j => j.status === 'running');
  if (running) await watchJob(running);
  else if (state.activeJob) await watchJob(await api('/api/jobs/' + state.activeJob.id));
}
export async function cancelActiveJob() {
  const id=state.active; if (!id) return;
  const result=await post('/api/jobs/' + id + '/cancel');
  if (state.active === id && result.status !== 'running') await finish(result,watchSequence);
}
export function returnToRun() {
  const job=state.activeJob; if (!job) return;
  emit('ui:navigate',job.request.task_id ? {view:'chat',projectId:job.request.project_id,taskId:job.request.task_id} : {view:'runs'});
}

export function restorePreview() { if (cachedPreview && state.active === cachedPreview.job.id) showAnswerPreview(cachedPreview.job,cachedPreview.preview); }
