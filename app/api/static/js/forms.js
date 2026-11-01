import {api, post, pending, jsonMutation} from './api.js?v=workspace-4-2';
import {state, emit} from './state.js?v=workspace-4-2';
import {$, node, action, handleAction, notify} from './ui.js?v=workspace-4-2';

function field(label, control) { const wrapper = node('label', label); wrapper.append(control); return wrapper; }
function input(name, placeholder = '') { const e = node('input'); e.name = name; e.placeholder = placeholder; return e; }
function select(name, choices) { const e = node('select'); e.name = name; for (const [value, label] of choices) e.append(new Option(label, value)); return e; }
let formSequence=0;
function form(title, label) {
  const sequence=++formSequence;
  const dialog = $('#editor-dialog'); if (dialog.open) dialog.close();
  $('#editor-title').textContent = title; $('#editor-error').textContent = '';
  $('#editor-body').replaceChildren(); $('#editor-submit').textContent = label;
  $('#editor-submit').disabled = false; $('#editor-submit').hidden=false; $('#editor-form').oninput = null; $('#editor-form').onsubmit = null;
  $('#editor-close').onclick = () => dialog.close();
  dialog.showModal(); return {dialog, alive:() => formSequence === sequence && dialog.open, body:$('#editor-body'), form:$('#editor-form'), submit:$('#editor-submit')};
}
function bind(editor, work) {
  let submitting = false;
  editor.form.onsubmit = async event => {
    event.preventDefault(); if (submitting || !editor.form.reportValidity()) return;
    submitting = true; editor.submit.disabled = true; $('#editor-error').textContent = '';
    const data=new FormData(editor.form);
    const controls=[...editor.form.elements].filter(e => e.id !== 'editor-close');
    const disabled=controls.map(e => e.disabled); controls.forEach(e => { e.disabled=true; });
    try { await work(data); }
    catch (e) { if (editor.alive()) $('#editor-error').textContent = e.message; else notify(e.message, true); }
    finally { submitting = false; controls.forEach((e,i) => { e.disabled=disabled[i]; }); if (editor.alive()) { editor.submit.disabled = false; editor.form.dispatchEvent(new Event('input')); } }
  };
}
function created(editor, projectId, taskId = null) {
  emit('workspace:reload');
  if (editor.alive()) { editor.dialog.close(); emit('ui:navigate', {view:'chat', projectId, taskId}); }
  else notify('创建结果已保留，可从项目树打开。');
}
export function showTaskForm(project) {
  const editor = form('新建任务 · ' + project.name, '创建任务');
  const intent = pending('create-task:' + project.project_id)?.payload;
  const name = input('name', '任务名称，支持中文'); name.required = true; name.maxLength = 200;
  editor.body.append(field('任务名称', name));
  const groups = [
    ['task_mode','任务模式',[['repository','仓库分析报告'],['knowledge','知识资料问答']]],
    ['memory_enabled','项目记忆',[['true','启用：允许读取与记录'],['false','关闭：仍保存聊天记录']]],
    ['context_policy','长对话策略',[['window','保留最近对话：可能舍弃早期上下文'],['full','保留完整历史：超出容量时报错'],['summary','摘要与任务状态：会整理历史，质量仍有限制']]],
  ];
  for (const [key, label, choices] of groups) {
    const group = node('fieldset'); group.append(node('legend', label));
    for (const [value, text] of choices) {
      const radio = input(key); radio.type = 'radio'; radio.value = value; radio.required = true;
      radio.disabled = key === 'task_mode' && value === 'repository' && project.project_kind !== 'repository';
      if (intent && String(intent[key]) === value) radio.checked = true;
      const item = field(text, radio); item.className = 'radio-choice'; group.append(item);
    }
    if (key === 'task_mode' && project.project_kind !== 'repository') group.append(node('p', '纯资料项目不能分析仓库，请另建仓库项目。', 'helper'));
    editor.body.append(group);
  }
  if (intent) name.value = intent.name;
  editor.body.append(node('p', '创建后以上三项配置不能修改；需要不同配置时请新建任务。', 'helper'));
  const ready = () => { editor.submit.disabled = !name.value.trim() || groups.some(([key]) => !editor.form.querySelector(`input[name="${key}"]:checked`)); };
  editor.form.oninput = ready; ready();
  bind(editor, async data => {
    const payload = {name:name.value.trim(), task_mode:data.get('task_mode'), memory_enabled:data.get('memory_enabled') === 'true', context_policy:data.get('context_policy')};
    const task = await jsonMutation('create-task:' + project.project_id, `/api/projects/${project.project_id}/tasks`, payload);
    created(editor, project.project_id, task.task_id);
  });
}
export async function rename(record, task = false) {
  const editor = form(task ? '重命名任务' : '重命名项目', '保存名称');
  const name = input('name'); name.value = record.name; name.required = true; name.maxLength = 200;
  editor.body.append(field('名称', name));
  bind(editor, async () => {
    await post(`/api/${task ? 'tasks' : 'projects'}/${task ? record.task_id : record.project_id}`, {name:name.value.trim()}, 'PATCH');
    editor.dialog.close(); emit('workspace:reload', {refresh:true});
  });
}
function githubKey(value) {
  try { const url = new URL(value); if (url.hostname.toLowerCase() !== 'github.com') return null;
    return 'github:' + url.pathname.replace(/^\/|\/$/g, '').replace(/\.git$/i, '').toLowerCase();
  } catch { return null; }
}
async function importSource(url) {
  if (state.active) throw new Error('请等待当前运行结束后再获取源码。');
  const job = await post('/api/repositories/import', {url}); emit('job:watch', {job});
  for (let count = 0; count < 130; count++) {
    await new Promise(resolve => setTimeout(resolve, 1000));
    const current = await api('/api/jobs/' + job.id);
    if (current.status === 'completed') return job.id;
    if (current.status !== 'running') throw new Error(current.error || '源码获取未完成，原记录已保留。');
  }
  throw new Error('源码仍在获取，请查看运行记录；不要重复启动。');
}
export async function showProjectForm() {
  const editor = form('新建项目', '创建项目');
  const old = pending('create-project')?.payload;
  const kind = select('source_kind', [['','请选择来源'],['local','启动时注册的本地仓库'],['github','公开 GitHub 仓库'],['collection','纯资料项目']]); kind.required = true;
  const name = input('name', '项目名称'); name.required = true; name.maxLength = 200;
  const url = input('url', 'https://github.com/owner/repo'); url.type = 'url';
  const github = node('div'); github.append(field('公开仓库链接', url));
  const independent = input('independent'); independent.type = 'checkbox';
  const independentLabel = field('明确新建同来源的独立项目（记忆互不共享）', independent); independentLabel.className = 'check';
  const matches = node('div');
  let lookupSequence=0;
  async function lookup() {
    const seq=++lookupSequence;
    matches.replaceChildren();
    let items = [];
    if (kind.value === 'local') items = await api('/api/projects?source_kind=local&repository_id=startup');
    if (kind.value === 'github') {
      const key = githubKey(url.value.trim());
      items = state.projects.filter(x => x.source_key === key);
    }
    if (!editor.alive() || seq !== lookupSequence) return;
    if (items.length) {
      matches.append(node('p', '此来源已有项目，优先打开；需要独立记忆时再勾选独立创建。', 'helper'));
      for (const p of items) matches.append(action('打开 ' + p.name, () => created(editor, p.project_id)));
    }
    return items;
  }
  const refresh = handleAction(async () => { github.hidden = kind.value !== 'github'; url.required=kind.value === 'github'; independentLabel.hidden = kind.value === 'collection' || !kind.value; await lookup(); });
  kind.onchange = refresh; url.onchange = refresh;
  if (old) { kind.value = old.source_kind; name.value = old.name; independent.checked = old.create_independent; }
  editor.body.append(field('项目来源', kind), github, action('查找已有项目', lookup), matches, field('新项目名称', name), independentLabel);
  if (old) editor.body.append(action('核对上次创建',async () => {
    const project=await jsonMutation('create-project','/api/projects',old);
    state.config.workspace_id=project.workspace_id; created(editor,project.project_id);
  }));
  await refresh();
  bind(editor, async () => {
    const existing = await lookup();
    if (existing?.length && !independent.checked) throw new Error('请打开已有项目，或明确选择新建独立项目。');
    let repository_id = kind.value === 'local' ? 'startup' : null;
    if (kind.value === 'github') {
      if (!url.value.trim()) throw new Error('请填写公开 GitHub 仓库链接。');
      repository_id = await importSource(url.value.trim());
    }
    const payload = {name:name.value.trim(), source_kind:kind.value, repository_id, create_independent:independent.checked && kind.value !== 'collection'};
    const project = await jsonMutation('create-project', '/api/projects', payload);
    state.config.workspace_id = project.workspace_id;
    created(editor, project.project_id);
  });
}
export async function showProjectDetails() {
  const project = state.project; if (!project) return;
  const editor = form('项目与来源 · ' + project.name, '应用到当前项目');
  editor.body.append(node('p', project.project_kind === 'repository' ? project.source_locator : '纯资料项目 · 不绑定仓库', 'helper'));
  if (project.source_kind !== 'github') {
    editor.submit.hidden = true;
    editor.dialog.addEventListener('close', () => { editor.submit.hidden = false; }, {once:true});
    return;
  }
  let replacement=null;
  const status=node('p','需要更新时，重新获取当前仓库，再明确应用。','helper');
  const pull=action('重新获取该仓库',async () => {
    const id=await importSource(project.source_locator);
    if (!editor.alive()) return;
    replacement=id; status.textContent='仓库新内容已获取，点击应用后用于当前项目的后续运行。'; editor.submit.disabled=false;
  });
  pull.disabled=!!state.active;
  editor.body.append(pull,status,node('p','仅影响当前项目的后续运行，旧历史和其他独立项目保留原来源。','helper'));
  editor.form.oninput=() => { editor.submit.disabled=!replacement; };
  editor.submit.disabled=true;
  bind(editor, async () => {
    if (!replacement) throw new Error('请先获取当前仓库的新内容。');
    await post(`/api/projects/${project.project_id}/repository-binding`, {repository_id:replacement}, 'PUT');
    editor.dialog.close(); emit('workspace:reload', {refresh:true});
  });
}
