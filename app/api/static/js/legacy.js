import {api, post} from './api.js?v=workspace-4-2';
import {state, checkpoint} from './state.js?v=workspace-4-2';
import {$, node, action, badge, empty, notify} from './ui.js?v=workspace-4-2';

let sequence=0, detailSequence=0;
function scope() {
  const q=new URLSearchParams({engine:$('#legacy-engine').value,mode:$('#legacy-mode').value,history:$('#legacy-memory-history').checked});
  if ($('#legacy-engine').value === 'repository' && $('#legacy-repository').value) q.set('repository',$('#legacy-repository').value);
  return q;
}
export async function loadLegacy() {
  const current=checkpoint(), seq=++sequence, kind=$('#legacy-kind').value; ++detailSequence;
  $('#legacy-detail').replaceChildren(); $('#legacy-filters').hidden=kind !== 'memories'; $('#legacy-upload-label').hidden=kind !== 'documents';
  const root=$('#legacy-list'); empty(root,'正在读取改造前范围…');
  const rows=await api(kind === 'histories' ? '/api/legacy/histories' : kind === 'documents' ? '/api/documents' : '/api/memories?' + scope());
  if (!current() || seq !== sequence) return;
  root.replaceChildren(); if (!rows.length) empty(root,'当前旧范围没有记录。新项目的数据请从项目入口查看。');
  for (const item of rows) {
    const row=node('div',undefined,'list-item'), info=node('div');
    if (kind === 'histories') {
      const type={session:'项目对话',repository:'仓库分析报告',knowledge:'知识资料问答'}[item.engine] || '未知类型';
      info.append(node('strong',item.session || '无法识别的旧记录'),node('p',type + ' · ' + (item.mode === 'live' ? '真实模型' : item.mode === 'offline' ? '本地演示' : '未知环境') + ' · 只读'));
      if (item.error_code) info.append(node('p','记录异常：' + item.error_code));
      row.append(info,action('查看旧对话',async () => {
        const d=++detailSequence, data=await api('/api/legacy/histories/' + item.legacy_id);
        if (!current() || seq !== sequence || d !== detailSequence) return;
        const detail=$('#legacy-detail'); detail.replaceChildren(node('h3','改造前对话 · 只读'));
        for (const message of data) { const article=node('article',undefined,'message ' + message.role); article.append(node('strong',message.role === 'user' ? '你' : 'ProjectPilot'),node('pre',message.content)); detail.append(article); }
      }));
    } else if (kind === 'documents') {
      info.append(node('strong',item.name),node('p','旧共享范围 · 不属于当前项目'));
      row.append(info,action('预览',async () => {
        const d=++detailSequence, data=await api('/api/documents/' + encodeURIComponent(item.name)); if (!current() || seq !== sequence || d !== detailSequence) return;
        $('#legacy-detail').replaceChildren(node('h3',item.name + ' · 旧共享资料'),node('pre',data.text));
      }));
    } else {
      info.append(node('strong',item.value),node('p','旧范围来源：' + item.source.session_id + '\n原句：' + item.source.quote)); row.append(info,badge(item.status));
      if (item.status === 'active') { const q=scope().toString(); const revoke=action('撤回旧范围记忆',async () => { await post('/api/memories/' + item.memory_id + '/revoke?' + q); notify('旧范围记忆已撤回，没有迁入项目。'); if (current()) await loadLegacy(); }); revoke.disabled=!!state.active; row.append(revoke); }
    }
    root.append(row);
  }
}
export async function loadLegacySources() {
  const sources=await api('/api/repositories');
  $('#legacy-repository').replaceChildren(...sources.map(r => new Option(r.id ? r.name + ' · ' + r.id.slice(0,8) : '启动时注册仓库',r.id)));
}
export async function uploadLegacy() {
  const input=$('#legacy-upload'), file=input.files[0]; if (!file) return;
  const current=checkpoint();
  await api('/api/documents/' + encodeURIComponent(file.name),{method:'PUT',headers:{'Content-Type':'application/octet-stream'},body:file}); input.value='';
  notify('资料上传到旧共享范围，未加入任何新项目。'); if (current()) await loadLegacy();
}
