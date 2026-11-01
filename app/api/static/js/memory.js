import {api, jsonMutation} from './api.js?v=workspace-4-2';
import {state, checkpoint} from './state.js?v=workspace-4-2';
import {$, node, action, empty, badge, notify, choose} from './ui.js?v=workspace-4-2';

let sequence=0;
export async function loadMemory() {
  const current=checkpoint(), seq=++sequence, projectId=state.projectId, environment=$('#memory-mode').value;
  const root=$('#memory-list');
  if (!projectId || !environment) { empty(root,!projectId ? '请先选择项目。' : '请选择要查看的记忆环境。'); return; }
  const records=await api(`/api/projects/${projectId}/memories?environment=${environment}&history=${$('#memory-history').checked}`);
  if (!current() || seq !== sequence) return;
  root.replaceChildren(); if (!records.length) empty(root,'当前项目及环境内没有相应记忆。');
  const busy=state.activeJob?.request?.project_id === projectId;
  for (const record of records) {
    const row=node('div',undefined,'list-item'), info=node('div');
    const sourceTask=(state.tasks.get(projectId) || []).find(t => 'projectpilot:' + t.task_id === record.source.session_id);
    info.append(node('strong',record.value),node('p',(record.key === 'project.model' ? '项目模型决定' : '回答偏好') + ' · 来源：' + (sourceTask?.name || record.source.session_id) + '\n原句：' + record.source.quote));
    if (sourceTask) info.append(action('打开来源任务',() => window.dispatchEvent(new CustomEvent('ui:navigate',{detail:{view:'chat',projectId,taskId:sourceTask.task_id}}))));
    const detail=node('details'); detail.append(node('summary','来源定位'),node('p',record.source.message_id)); info.append(detail);
    row.append(info,record.expires_at && Date.parse(record.expires_at) <= Date.now() ? node('span','已过期','badge') : badge(record.status));
    if (['active','candidate'].includes(record.status)) {
      const revoke=action('撤回',async () => {
        const response=await choose('撤回项目记忆','撤回后不再用于后续召回，不恢复之前被替代的值。',[['yes','撤回'],['cancel','取消']]);
        if (response !== 'yes') return;
        await jsonMutation('revoke:' + record.memory_id,`/api/projects/${projectId}/memories/${record.memory_id}/revoke`,{environment,expected_revision:record.revision});
        notify('记忆已撤回，历史记录仍保留。'); if (current()) await loadMemory();
      });
      revoke.disabled=busy; revoke.title=busy ? '请等待本项目当前运行结束' : ''; row.append(revoke);
    }
    root.append(row);
  }
  if (busy) root.prepend(node('p','本项目有运行尚未结束，当前可以查看，结束后再撤回。','helper'));
}
