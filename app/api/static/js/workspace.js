import {api, post} from './api.js?v=workspace-4-2';
import {state, saved, save, emit, checkpoint} from './state.js?v=workspace-4-2';
import {$, node, action, handleAction, empty} from './ui.js?v=workspace-4-2';
import {showTaskForm, showProjectForm, showProjectDetails, rename} from './forms.js?v=workspace-4-2';

export {showTaskForm, showProjectForm, showProjectDetails};
export async function loadTree() {
  const current = checkpoint();
  const projects = await api('/api/projects');
  const groups = await Promise.all(projects.map(async p => [p.project_id, await api(`/api/projects/${p.project_id}/tasks`)]));
  if (!current()) return;
  state.projects = projects; state.tasks = new Map(groups);
  if (projects.length) state.config.workspace_id = projects[0].workspace_id;
  renderTree();
}
export function renderTree() {
  const root = $('#project-tree'); root.replaceChildren();
  const hasProjects=state.projects.length > 0;
  root.hidden=!hasProjects;
  $('#project-tree-label').hidden=!hasProjects;
  $('#workspace-navigation').hidden=!hasProjects;
  if (!hasProjects) return;
  const collapsed = saved('collapsed', []);
  for (const project of state.projects) {
    const id = project.project_id;
    const group = node('div', undefined, 'project-group');
    const row = node('div', undefined, 'project-row' + (state.projectId === id ? ' selected' : ''));
    const toggle = action(collapsed.includes(id) ? '▸' : '▾', () => {
      const next = saved('collapsed', []);
      save('collapsed', next.includes(id) ? next.filter(x => x !== id) : [...next, id]); renderTree();
    });
    toggle.setAttribute('aria-label', '展开或收起 ' + project.name);
    toggle.setAttribute('aria-expanded', String(!collapsed.includes(id)));
    const title = action(project.name + (project.project_kind === 'repository' ? '［仓库］' : '［资料］'), () => emit('ui:navigate', {view:'chat', projectId:id, taskId:null}), 'tree-title');
    const edit = action('⋯', () => rename(project)); edit.setAttribute('aria-label', '重命名项目 ' + project.name);
    row.append(toggle, title, edit); group.append(row);
    if (!collapsed.includes(id)) {
      const children = node('div', undefined, 'task-rows');
      for (const task of state.tasks.get(id) || []) {
        const taskRow = node('div', undefined, 'task-row' + (task.task_id === state.taskId ? ' selected' : ''));
        const open = action(task.name, () => emit('ui:navigate', {view:'chat', projectId:id, taskId:task.task_id}), 'tree-title');
        if (state.activeJob?.request?.task_id === task.task_id) open.append(node('span', ' · 运行中', 'running-mark'));
        const taskEdit = action('⋯', () => rename(task, true)); taskEdit.setAttribute('aria-label', '重命名任务 ' + task.name);
        taskRow.append(open, taskEdit); children.append(taskRow);
      }
      children.append(action('＋ 新建任务', () => showTaskForm(project)));
      group.append(children);
    }
    root.append(group);
  }
}
export function projectOverview() {
  $('#task-heading').textContent = state.task ? state.project.name + ' / ' + state.task.name : state.project?.name || '选择一个项目';
  $('#project-details').hidden = !state.project;
  $('#new-task').hidden = !state.project;
  $('#project-overview').hidden = !!state.task;
  $('#task-content').hidden = !state.task;
  if (!state.task) {
    const root = $('#project-overview'); root.replaceChildren();
    if (!state.project) { root.append(node('h2', '从一个项目开始'), node('p', '选择侧边栏已有项目，或创建仓库／资料项目。', 'helper'), action('＋ 新建项目', showProjectForm, 'primary')); }
    else {
      root.append(node('h2', '项目任务'), node('p', state.project.project_kind === 'repository' ? state.project.source_locator : '独立资料集合', 'helper'));
      const tasks = state.tasks.get(state.projectId) || [];
      if (!tasks.length) root.append(node('p', '还没有任务。创建时主动选择模式、记忆权限和长对话策略。', 'helper'));
      for (const task of tasks) root.append(action(task.name, () => emit('ui:navigate', {view:'chat', taskId:task.task_id}), 'task-link'));
      root.append(action('＋ 新建任务', () => showTaskForm(state.project), 'primary'));
    }
    $('#task-subtitle').textContent = state.project ? '任务之间保持独立对话，项目资料和记忆按项目管理。' : '项目下的每个任务是一段独立对话。';
  }
  for (const element of document.querySelectorAll('.project-context')) element.textContent = state.project ? '当前项目：' + state.project.name : '请先在侧边栏选择项目。';
}
