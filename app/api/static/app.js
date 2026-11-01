import {api} from './js/api.js?v=workspace-4-2';
import {state, saved, save, emit, viewTitles, selectionDirty} from './js/state.js?v=workspace-4-2';
import {$, $$, node, listen, handleAction, notify} from './js/ui.js?v=workspace-4-2';
import {loadTree, renderTree, projectOverview, showProjectForm, showTaskForm, showProjectDetails} from './js/workspace.js?v=workspace-4-2';
import {renderTask, renderEnvironment, loadHistory, submitChat, checkSend, saveDraft, updateSendState} from './js/chat.js?v=workspace-4-2';
import {loadSelection, renderSelection, selectionChanged, applySelection, reloadSelection, leaveSelection, loadDocuments, uploadDocument} from './js/documents.js?v=workspace-4-2';
import {watchJob, restoreRunningJob, cancelActiveJob, returnToRun, renderActive, restorePreview} from './js/jobs.js?v=workspace-4-2';
import {loadMemory} from './js/memory.js?v=workspace-4-2';
import {loadRuns} from './js/runs.js?v=workspace-4-2';
import {loadEvaluations, submitEvaluation} from './js/evaluations.js?v=workspace-4-2';
import {loadLegacy, loadLegacySources, uploadLegacy} from './js/legacy.js?v=workspace-4-2';

let navigating=0, taskRefreshSequence=0, treeRefreshSequence=0, memoryContext='';
function treeVisibility(open = document.body.classList.contains('tree-open')) {
  document.body.classList.toggle('tree-open',open);
  $('#tree-toggle').setAttribute('aria-expanded',String(open));
  $('#sidebar').inert=window.matchMedia('(max-width:850px)').matches && !open;
}
async function loadBacklog() {
  const current=state.epoch, data=await api('/api/backlog');
  if (state.epoch !== current) return;
  $('#backlog-count').textContent=data.items.length; $('#backlog-policy').textContent=data.policy;
  $('#backlog-list').replaceChildren(...data.items.map(item => {
    const article=node('article',undefined,'backlog-card'); article.append(node('span',item.phase,'badge'),node('h3',item.title),node('p',item.detail),node('small',item.status === 'deferred' ? '已记录 · 暂缓处理' : '后续完善')); return article;
  }));
}
const loaders={knowledge:loadDocuments,memory:loadMemory,runs:loadRuns,evaluations:loadEvaluations,legacy:loadLegacy,backlog:loadBacklog};
function routeHash(view, projectId, taskId) {
  const q=new URLSearchParams(); if (projectId) q.set('project',projectId); if (taskId) q.set('task',taskId);
  return '#' + view + (q.size ? '?' + q : '');
}
function readRoute() {
  const [view,search='']=location.hash.slice(1).split('?'); const q=new URLSearchParams(search);
  return {view:viewTitles[view] ? view : 'chat',projectId:q.get('project'),taskId:q.get('task')};
}
async function activate(route) {
  const epoch=++state.epoch; saveDraft();
  state.view=route.view; state.projectId=route.projectId || null; state.taskId=route.taskId || null; state.task=null; state.project=null;
  $('#mode').disabled=true;
  $$('.panel').forEach(p => { p.hidden=p.id !== 'panel-' + state.view; });
  $$('nav [data-view]').forEach(b => b.classList.toggle('active',b.dataset.view === state.view));
  $('#view-title').textContent=viewTitles[state.view]; document.title='ProjectPilot · ' + viewTitles[state.view];
  $('#task-content').hidden=true; $('#project-overview').hidden=false; $('#project-overview').replaceChildren(node('p','正在读取项目与任务…','helper'));
  $('#run-detail').replaceChildren(node('p','选择一条运行记录。','helper'));
  for (const id of ['messages','documents','memory-list','legacy-list']) $('#' + id).replaceChildren();
  treeVisibility(false);
  await loadTree(); if (state.epoch !== epoch) return;
  let identified=false;
  try {
    const [project,task]=await Promise.all([route.projectId ? api('/api/projects/' + route.projectId) : null,route.taskId ? api('/api/tasks/' + route.taskId) : null]);
    if (state.epoch !== epoch) return;
    if (task && (!project || task.project_id !== project.project_id)) throw new Error('任务不属于当前项目，请从项目树重新选择。');
    state.project=project; state.task=task; identified=true; renderEnvironment();
    const context=state.projectId + ':' + state.taskId + ':' + (task?.run_environment || '');
    if (memoryContext !== context) { $('#memory-mode').value=task?.run_environment || ''; memoryContext=context; }
    projectOverview(); renderTree(); renderActive();
    if (task) {
      renderTask();
      if (task.task_mode === 'knowledge') await loadSelection();
      if (state.epoch !== epoch) return;
      if (state.view === 'chat') { await loadHistory(); if (state.epoch === epoch) restorePreview(); }
    }
    if (state.epoch !== epoch) return;
    if (loaders[state.view]) await loaders[state.view]();
    if (state.epoch === epoch) save('route',route);
  } catch (error) {
    if (state.epoch !== epoch) return;
    if (!identified) { state.task=null; state.taskId=null; state.project=null; state.projectId=null; projectOverview(); renderTree(); renderEnvironment(); }
    notify(error.message,true);
  }
}
async function navigate(detail, replace=false) {
  const token=++navigating;
  const route={view:detail.view || state.view,projectId:Object.hasOwn(detail,'projectId') ? detail.projectId : state.projectId,taskId:Object.hasOwn(detail,'taskId') ? detail.taskId : state.taskId};
  if (!viewTitles[route.view]) route.view='chat';
  if (selectionDirty() && !(await leaveSelection())) return;
  if (token !== navigating) return;
  history[replace ? 'replaceState' : 'pushState'](null,'',routeHash(route.view,route.projectId,route.taskId));
  await activate(route);
}
async function refreshTask() {
  if (!state.taskId) return;
  const taskId=state.taskId, epoch=state.epoch, seq=++taskRefreshSequence;
  const task=await api('/api/tasks/' + taskId);
  if (state.epoch !== epoch || seq !== taskRefreshSequence || state.taskId !== taskId) return;
  state.task=task; projectOverview(); renderTask(false); updateSendState();
}
async function refreshWorkspace(refreshIdentity=false) {
  const epoch=state.epoch, seq=++treeRefreshSequence; await loadTree();
  if (state.epoch !== epoch || seq !== treeRefreshSequence) return;
  if (refreshIdentity && state.projectId) { const project=await api('/api/projects/' + state.projectId); if (state.epoch !== epoch || seq !== treeRefreshSequence) return; state.project=project; projectOverview(); await refreshTask(); }
  renderActive();
}
function on(name,handler) { window.addEventListener(name,handleAction(event => handler(event.detail))); }
function bindEvents() {
  listen('#new-project','click',showProjectForm); listen('#new-task','click',() => showTaskForm(state.project)); listen('#project-details','click',showProjectDetails);
  listen('#chat-form','submit',submitChat); listen('#check-send','click',checkSend); listen('#prompt','input',saveDraft);
  listen('#mode','change',() => { const value=$('#mode').value; save('preferred-environment',value); if (state.task && !state.task.run_environment) save('environment.' + state.taskId,value); updateSendState(); });
  listen('#apply-selection','click',applySelection); listen('#reload-selection','click',reloadSelection);
  listen('#retriever','change',() => { const edit=state.selections.get(state.taskId); if (edit) edit.retriever=$('#retriever').value; selectionChanged(); });
  listen('#upload','change',uploadDocument); listen('#memory-mode','change',loadMemory); listen('#memory-history','change',loadMemory); listen('#refresh-memory','click',loadMemory);
  listen('#refresh-runs','click',loadRuns); listen('#refresh-evaluations','click',loadEvaluations);
  listen('#select-all-cases','click',() => { for (const option of $('#eval-cases').options) option.selected=true; });
  listen('#eval-form','submit',async event => { if (state.active) { event.preventDefault(); return; } await watchJob(await submitEvaluation(event)); });
  for (const id of ['legacy-kind','legacy-engine','legacy-mode','legacy-repository','legacy-memory-history']) listen('#' + id,'change',loadLegacy);
  listen('#legacy-upload','change',uploadLegacy); listen('#cancel-job','click',cancelActiveJob); listen('#return-run','click',returnToRun);
  listen('#tree-toggle','click',() => treeVisibility(!document.body.classList.contains('tree-open')));
  listen('#tree-close','click',() => { treeVisibility(false); $('#tree-toggle').focus(); });
  window.addEventListener('resize',() => treeVisibility());
  window.addEventListener('keydown',event => { if (event.key === 'Escape' && !document.querySelector('dialog[open]')) treeVisibility(false); });
  document.addEventListener('click',handleAction(event => { const control=event.target.closest('[data-view]'); if (!control) return; event.preventDefault(); return navigate({view:control.dataset.view}); }));
  on('ui:navigate',navigate); on('ui:notice',detail => notify(detail.message,detail.error)); on('job:watch',({job}) => watchJob(job));
  on('workspace:reload',detail => refreshWorkspace(detail.refresh)); on('workspace:render',renderTree); on('task:refresh',refreshTask); on('task:status',updateSendState);
  on('busy:changed',async () => { updateSendState(); renderActive(); renderTree(); if (state.view === 'memory') await loadMemory(); });
  on('run:finished',async () => { if (state.view === 'runs') await loadRuns(); if (state.view === 'evaluations') await loadEvaluations(); });
  window.addEventListener('popstate',handleAction(async () => { if (selectionDirty() && !(await leaveSelection())) { history.pushState(null,'',routeHash(state.view,state.projectId,state.taskId)); return; } await activate(readRoute()); }));
  window.addEventListener('beforeunload',event => { saveDraft(); if ([...state.selections.keys()].some(id => selectionDirty(id))) { event.preventDefault(); event.returnValue=''; } });
  window.addEventListener('focus',handleAction(async () => { if (!state.config) return; await restoreRunningJob(); await refreshTask(); }));
}
async function initialize() {
  try {
    state.config=await api('/api/config');
    if (!state.config.storage_namespace) throw new Error('页面已更新，请重启工作台服务后继续使用。');
    $('#connection').textContent='本地已连接';
    $('#eval-mode option[value="live"]').disabled=!state.config.live_enabled;
    bindEvents();
    await loadLegacySources();
    const route=location.hash ? readRoute() : saved('route',{view:'chat',projectId:null,taskId:null});
    await navigate(route,true); await restoreRunningJob();
  } catch (error) { $('#connection').textContent='连接失败'; notify(error.message,true); }
}
initialize();
