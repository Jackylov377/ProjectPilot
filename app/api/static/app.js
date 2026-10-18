"use strict";
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = {view: "chat", documents: new Set(), active: null, events: null, jobs: [], config: null};
const titles = {chat: "工作对话", knowledge: "知识资料", runs: "运行记录", evaluations: "评测中心", memory: "项目记忆", backlog: "待办与缺陷"};
const statuses = {running: "运行中", completed: "执行完成", failed: "失败", cancelled: "已停止", interrupted: "已中断", passed: "通过", review_required: "待复核", infrastructure_error: "执行设施错误", active: "有效", candidate: "候选", superseded: "已替代", revoked: "已撤回", duplicate: "重复"};
const caseTitles = {"note-read":"读取项目介绍", "note-missing":"资料缺失时说明不足", "knowledge-fact":"检索校准间隔", "knowledge-unanswerable":"无答案检索", "knowledge-update":"资料更新后重新问答", "memory-model-change":"跨会话模型决定变更", "repository-report":"仓库分析与报告", "document-injection":"文档指令干扰", "note-stack":"按要求选择技术资料", "note-empty":"空资料不编造", "note-repair":"资料恢复后继续", "note-oversized":"资料超过读取上限", "retrieval-two-sources":"两个来源共同作答", "retrieval-related-no-answer":"相关资料没有数值答案", "retrieval-scope-collision":"训练与推理配置区分", "retrieval-deletion":"删除资料后重新问答", "retrieval-selected-documents":"未选资料不进入检索", "retrieval-historical-value":"历史与现行值区分", "memory-hypothetical":"假设不作为决定", "memory-negated":"否定不作为决定", "memory-language":"跨会话语言偏好", "memory-question-not-decision":"提问不作为决定", "memory-revert-decision":"模型方案改回旧值", "memory-document-not-user":"文档建议不自动记忆", "session-history-isolation":"不同会话历史隔离", "context-early-constraints":"长历史保留早期约束", "context-task-progress":"摘要后继续任务进度", "context-source-boundary":"摘要后区分计划与执行", "repository-readme-repair":"仓库说明恢复后分析", "repository-no-function":"没有实现时不编造"};
const metricTitles = {required_tool_coverage:"必需工具名称覆盖", first_search_recall_at_3:"首次检索证据召回@3", first_search_mrr_at_3:"首次检索排序@3", unanswerable_empty_retrieval:"无答案题空检索比例", annotated_memory_recall:"标注记忆召回", annotated_context_retention:"实际请求信息保留", annotated_response_constraints:"回答约束满足", recovery_task_success:"恢复任务严格成功"};

function node(tag, text, className) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; if (className) item.className = className; return item; }
function notify(message, error = false) { const box = $("#notice"); box.textContent = message; box.className = error ? "error" : ""; box.hidden = false; clearTimeout(notify.timer); notify.timer = setTimeout(() => box.hidden = true, 6500); }
async function api(path, options = {}) { const response = await fetch(path, options); const data = await response.json(); if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "提交内容有误，请检查输入。"); return data; }
function post(path, data) { return api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data || {})}); }
function listen(selector, event, action) { $(selector).addEventListener(event, async (e) => { try { await action(e); } catch (error) { notify(error.message, true); } }); }
function badge(status) { return node("span", statuses[status] || status, "badge " + status); }
function empty(container, message) { container.replaceChildren(node("p", message, "empty-copy")); }
function date(value) { return new Date(value).toLocaleString("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}); }
function repositoryScope(engine) { const id = $("#repository-select").value; return engine === "repository" && id ? "&repository=" + encodeURIComponent(id) : ""; }
function scope() { return "engine=" + encodeURIComponent($("#engine").value) + "&mode=" + $("#mode").value + repositoryScope($("#engine").value); }
function memoryScope() { return "engine=" + $("#memory-engine").value + "&mode=" + $("#mode").value + repositoryScope($("#memory-engine").value); }
function setBusy(busy) { $("#send").disabled = busy; $("#start-evaluation").disabled = busy; $("#import-repository").disabled = busy; $("#repository-select").disabled = busy; $("#cancel-job").hidden = !busy; $("#job-status").textContent = busy ? "任务运行中" : "就绪"; }
function selectionHint() {
  $("#selected-documents").textContent = [...state.documents].join("、") || "尚未选择";
  $("#knowledge-selection").hidden = $("#engine").value !== "knowledge";
  $("#repository-settings").hidden = $("#engine").value !== "repository";
  const selected = (state.repositories || []).find(item => item.id === $("#repository-select").value);
  const imported = $("#engine").value === "repository" && selected?.id;
  if (state.config) {
    $("#workspace-name").textContent = imported ? selected.name : state.config.workspace_name;
    $("#workspace-path").textContent = imported ? selected.url : state.config.workspace;
  }
}
async function loadRepositories(preferred) {
  state.repositories = await api("/api/repositories");
  const current = preferred ?? ($("#repository-select").value || localStorage.getItem("projectpilot.repository") || "");
  $("#repository-select").replaceChildren();
  state.repositories.forEach(item => $("#repository-select").append(new Option(item.id ? item.name : "启动时项目 · " + item.name, item.id)));
  $("#repository-select").value = state.repositories.some(item => item.id === current) ? current : "";
  localStorage.setItem("projectpilot.repository", $("#repository-select").value);
  selectionHint();
}

async function navigate(view) {
  if (!titles[view]) view = "chat";
  state.view = view; location.hash = view;
  $$(".panel").forEach(panel => panel.hidden = panel.id !== "panel-" + view);
  $$("nav button").forEach(button => button.classList.toggle("active", button.dataset.view === view));
  $("#view-title").textContent = titles[view]; document.title = "ProjectPilot · " + titles[view];
  if (view === "knowledge") await loadDocuments();
  if (view === "runs") await loadRuns();
  if (view === "evaluations") await loadEvaluations();
  if (view === "memory") await loadMemory();
  if (view === "backlog") await loadBacklog();
}

function addMessage(role, text) {
  const box = $("#messages"); if (box.querySelector(".empty-state")) box.replaceChildren();
  const message = node("article", undefined, "message " + role);
  message.append(node("span", role === "user" ? "你" : "ProjectPilot", "message-label"), node("div", text, "message-body"));
  box.append(message); box.scrollTop = box.scrollHeight;
}
const originalEmptyState = $("#messages").firstElementChild.cloneNode(true);
async function loadHistory() {
  const session = $("#session").value;
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(session)) return;
  const messages = await api("/api/sessions/" + session + "?" + scope());
  $("#messages").replaceChildren();
  if (!messages.length) $("#messages").append(originalEmptyState.cloneNode(true));
  else messages.forEach(message => addMessage(message.role, message.content));
}
async function loadSessions() {
  const sessions = await api("/api/sessions?" + scope());
  const current = $("#session").value;
  $("#session-list").replaceChildren();
  [...new Set([current, ...sessions.map(item => item.session)])].forEach(name => $("#session-list").append(new Option(name, name, false, name === current)));
  await loadHistory();
}
function progressLabel(event) {
  const imports = {"repository.import.started":"正在连接GitHub获取源码", "repository.downloaded":"源码已下载，正在解压", "repository.ready":"源码已就绪，可以开始分析", "repository.import.failed":"源码导入失败"};
  if (imports[event.event]) return imports[event.event];
  const names = {"run.started":"开始执行任务", "session.loaded":"已读取会话历史", "iteration.started":"正在处理问题", "tool.started":"正在使用工具", "tool.finished":"工具已返回结果", "llm.call":"模型请求已返回", "context.summary.saved":"已保存任务摘要", "memory.recalled":"已检索项目记忆", "session.verified":"会话已保存", "run.finished":"本次执行已结束"};
  const tools = {read_project_note:"读取项目笔记",list_files:"查看目录",read_file:"读取文件",search_code:"查找代码",write_report:"保存报告",search_knowledge:"检索资料"};
  return (names[event.event] || "正在执行任务") + (event.name ? " · " + (tools[event.name] || event.name) : "");
}
function watchJob(job) {
  if (state.events) state.events.close();
  state.active = job.id; setBusy(true);
  $("#progress-text").textContent = job.kind === "evaluation" ? "评测已开始，可在运行记录中查看状态。" : job.kind === "repository_import" ? "正在从GitHub获取源码，请稍候。" : "任务已提交，正在准备。";
  const stream = new EventSource("/api/jobs/" + job.id + "/events"); state.events = stream;
  stream.addEventListener("progress", event => { $("#progress-text").textContent = progressLabel(JSON.parse(event.data)); });
  stream.addEventListener("answer", event => {
    if (job.kind !== "agent" || job.request.session !== $("#session").value || job.request.engine !== $("#engine").value || job.request.mode !== $("#mode").value) return;
    const preview = JSON.parse(event.data);
    let article = $("#messages [data-preview]");
    if (!preview.text) { if (article) article.remove(); return; }
    if (!article) {
      article = node("article", undefined, "message assistant preview-message"); article.dataset.preview = job.id;
      article.append(node("span", "ProjectPilot · 生成预览，以最终保存结果为准", "message-label"), node("div", "", "message-body"));
      $("#messages").append(article);
    }
    article.querySelector(".message-body").textContent = preview.text;
    $("#messages").scrollTop = $("#messages").scrollHeight;
  });
  stream.addEventListener("done", async event => {
    stream.close(); state.events = null; state.active = null; setBusy(false);
    const finished = JSON.parse(event.data);
    $$("#messages [data-preview]").forEach(preview => preview.remove());
    $("#progress-text").textContent = finished.status === "completed" ? "运行已结束，结果已保存。" : finished.error || statuses[finished.status];
    try {
      if (finished.kind === "repository_import" && finished.status === "completed") {
        $("#engine").value = "repository";
        await loadRepositories(finished.id);
        $("#session").value = "repo-" + finished.id.slice(0,8);
        await loadSessions();
        notify("源码已导入，当前已选中该仓库。输入问题即可分析。");
      }
      if (finished.kind === "agent" && finished.request.session === $("#session").value && finished.request.engine === $("#engine").value && finished.request.mode === $("#mode").value) {
        await loadSessions();
        if (finished.result?.reports?.length) notify("报告已保存，可在运行详情中查看正文。");
      }
      if (state.view === "runs") await loadRuns();
      if (finished.kind === "evaluation" && state.view === "evaluations") await loadEvaluations();
      if (finished.status !== "completed") notify(finished.error || "本次运行未完成，记录已保留。", true);
      else if (finished.kind === "evaluation") notify("评测执行完成，具体通过情况请查看报告。");
    } catch (error) { notify(error.message, true); }
  });
  stream.onerror = () => { $("#progress-text").textContent = "正在重新连接进度，任务状态仍会保留。"; };
}

async function sendChat(event) {
  event.preventDefault(); if (state.active) return;
  const prompt = $("#prompt").value.trim(); if (!prompt) return;
  const job = await post("/api/chat", {prompt, session:$("#session").value,engine:$("#engine").value, mode:$("#mode").value, context_policy:$("#context-policy").value, memory:$("#use-memory").checked, documents:[...state.documents], repository:$("#engine").value === "repository" ? $("#repository-select").value || null : null});
  addMessage("user", prompt); $("#prompt").value = ""; watchJob(job);
}

async function loadDocuments() {
  const documents = await api("/api/documents"); $("#document-count").textContent = documents.length + " 份";
  const box = $("#documents"); box.replaceChildren();
  state.documents = new Set([...state.documents].filter(name => documents.some(item => item.name === name)));
  if (!documents.length) empty(box, "还没有资料，先导入一份文件。");
  documents.forEach(item => {
    const row = node("div", undefined, "list-item"); const check = document.createElement("input"); check.type = "checkbox"; check.checked = state.documents.has(item.name); check.setAttribute("aria-label", "选择 " + item.name);
    check.onchange = () => { check.checked ? state.documents.add(item.name) : state.documents.delete(item.name); selectionHint(); };
    const info = node("div"); info.append(node("strong", item.name), node("p", (item.bytes / 1024).toFixed(1) + " KB"));
    const preview = node("button", "预览", "text-button"); preview.onclick = async () => { try { const data = await api("/api/documents/" + encodeURIComponent(item.name)); $("#preview-title").textContent = item.name; $("#document-preview").textContent = data.text + (data.truncated ? "\n\n[预览仅显示前半部分]" : "") + (data.pages_without_text.length ? "\n\n部分PDF页面没有提取到文字。" : ""); } catch (error) { notify(error.message, true); } };
    row.append(check, info, preview); box.append(row);
  }); selectionHint();
}

async function loadRuns() {
  state.jobs = await api("/api/jobs"); const box = $("#runs-list"); box.replaceChildren();
  if (!state.jobs.length) empty(box, "还没有运行记录。发送一条问题即可开始。");
  state.jobs.forEach(job => {
    const row = node("div", undefined, "list-item selectable"); const info = node("div");
    const title = job.kind === "evaluation" ? "开发评测" : job.kind === "repository_import" ? "获取GitHub源码 · " + job.request.url : job.request.prompt;
    info.append(node("strong", title.slice(0,65)), node("p", date(job.created_at) + " · " + (job.kind === "evaluation" ? "固定任务集" : job.kind === "repository_import" ? "仓库导入" : job.request.session)));
    row.append(info,badge(job.status)); row.onclick = () => showRun(job.id).catch(error => notify(error.message,true)); box.append(row);
  });
}
async function showRun(id) {
  const job = await api("/api/jobs/" + id); const box = $("#run-detail"); box.replaceChildren(); box.classList.remove("empty-copy");
  box.append(badge(job.status),node("h4",job.kind === "agent" ? "任务与结果" : job.kind === "repository_import" ? "源码导入" : "评测执行"),node("pre",job.request.prompt || job.request.url || "配置：" + job.request.configs.join(" / ")));
  if (job.result?.repository) { const repo=job.result.repository; box.append(node("pre",repo.name + "\n默认分支快照 · " + repo.files + " 个文件\n提交：" + (repo.commit || "未提供提交标识") + "\n" + repo.url)); }
  if (job.status !== "running") {
    const download = node("a", "导出本次结果（ZIP）", "text-button");
    download.href = "/api/jobs/" + job.id + "/export"; download.setAttribute("download", ""); box.append(download);
  }
  if (job.error) box.append(node("pre", job.error));
  if (job.result?.answer) box.append(node("pre",job.result.answer));
  (job.result?.reports || []).forEach(report => { const detail=node("details"); detail.append(node("summary","已保存报告 · " + report.name),node("pre",report.text)); box.append(detail); });
  if (job.kind === "evaluation" && job.result?.summary) showGroups(box,job.result.summary.groups);
  const records = await api("/api/jobs/" + id + "/trace");
  if (records.length) {
    box.append(node("h4","执行过程")); const timeline = node("ul",undefined,"timeline");
    records.filter(item => ["run.started","tool.started","tool.finished","context.summary.saved","memory.recalled","run.finished","repository.import.started","repository.downloaded","repository.ready","repository.import.failed"].includes(item.event)).forEach(item => {const row=node("li",progressLabel(item)); row.append(node("span",(item.elapsed_ms / 1000).toFixed(2)+" 秒")); timeline.append(row);}); box.append(timeline);
    const raw=node("details"); raw.append(node("summary","查看详细过程数据"),node("pre",JSON.stringify(records,null,2))); box.append(raw);
  }
}

function showGroups(box,groups) {
  groups.forEach(group => {
    box.append(node("h3",group.config + " · " + group.planned + " 次任务"));
    const counts=node("div",undefined,"score-grid"); [["passed","通过"],["failed","失败"],["review_required","待复核"]].forEach(([key,label])=>{const item=node("div");item.append(node("strong",group[key]),node("small",label));counts.append(item);});box.append(counts);
    if (group.metrics) {const table=node("table",undefined,"metric-table");const head=node("tr");head.append(node("th","证据指标"),node("th","结果"));table.append(head);Object.entries(group.metrics).forEach(([key,value])=>{const row=node("tr");row.append(node("td",metricTitles[key]||key),node("td",value.value===null?"未测量":(value.value*100).toFixed(1)+"%（"+value.numerator+" / "+value.denominator+"）"));table.append(row);});box.append(table);}
  });
}
async function loadEvaluations() {
  const reports = await api("/api/evaluations"); const box=$("#evaluation-list");box.replaceChildren();
  if (!reports.length) empty(box,"还没有评测报告，可以先运行少量开发任务。");
  reports.forEach(report=>{const card=node("button",undefined,"evaluation-card");card.append(node("span",report.summary.mode==="offline"?"本地预设演示":"真实模型 · 开发集","badge"),node("h3",report.name));const totals=report.summary.groups.reduce((sum,g)=>({passed:sum.passed+g.passed,failed:sum.failed+g.failed,review:sum.review+g.review_required}),{passed:0,failed:0,review:0});card.append(node("p","通过 "+totals.passed+" · 失败 "+totals.failed+" · 待复核 "+totals.review));card.onclick=()=>showEvaluation(report.id,report.name).catch(error=>notify(error.message,true));box.append(card);});
  if (!$("#eval-cases").options.length) {const cases=await api("/api/evaluation-cases");cases.forEach(item=>$("#eval-cases").append(new Option(caseTitles[item.id]||item.id,item.id,false,["note-read","knowledge-fact","memory-model-change"].includes(item.id))));}
}
async function showEvaluation(id,name) {
  const report=await api("/api/evaluations/"+id);const box=$("#evaluation-detail");box.hidden=false;box.replaceChildren(node("h3",name),node("p",report.summary.interpretation,"helper"));showGroups(box,report.summary.groups);
  const issues=(report.tasks||[]).filter(task=>task.status!=="passed");box.append(node("h3","失败与待复核项"));if(!issues.length)box.append(node("p","没有需要关注的任务。","helper"));
  issues.forEach(task=>{const row=node("div",undefined,"list-item");const info=node("div");info.append(node("strong",(caseTitles[task.case_id]||task.case_id)+" · "+task.config),node("p",task.failures.join("\n")));row.append(info,badge(task.status));box.append(row);});box.scrollIntoView({behavior:"smooth",block:"start"});
}
async function loadMemory() {
  const records=await api("/api/memories?"+memoryScope()+"&history="+$("#memory-history").checked);const box=$("#memory-list");box.replaceChildren();if(!records.length)empty(box,"当前范围内还没有项目记忆。可在工作对话中启用记忆并明确说明项目决定。");
  records.forEach(record=>{const row=node("div",undefined,"list-item");const info=node("div");info.append(node("strong",record.value),node("p",(record.key==="project.model"?"项目模型决定":"回答偏好")+" · 来源："+record.source.session_id+"\n原句："+record.source.quote));row.append(info,badge(record.status));if(record.status==="active"){const button=node("button","撤回","text-button");button.disabled=!!state.active;button.onclick=async()=>{try{await post("/api/memories/"+record.memory_id+"/revoke?"+memoryScope());await loadMemory();notify("这条记忆已撤回，历史记录仍保留。");}catch(error){notify(error.message,true);}};row.append(button);}box.append(row);});
}
async function loadBacklog() {
  const data=await api("/api/backlog");$("#backlog-count").textContent=data.items.length;$("#backlog-policy").textContent=data.policy;const box=$("#backlog-list");box.replaceChildren();data.items.forEach(item=>{const card=node("article",undefined,"backlog-card");card.append(node("span",item.phase,"badge"),node("h3",item.title),node("p",item.detail),node("small",item.status==="deferred"?"已记录 · 暂缓处理":"后续完善"));box.append(card);});
}

listen("#chat-form","submit",sendChat);
listen("#cancel-job","click",async()=>{if(state.active)await post("/api/jobs/"+state.active+"/cancel");});
listen("#new-session","click",async()=>{$("#session").value="chat-"+Date.now().toString(36);await loadSessions();$("#prompt").focus();});
listen("#session-list","change",async()=>{$("#session").value=$("#session-list").value;await loadHistory();});
listen("#session","change",loadSessions);
listen("#engine","change",async()=>{selectionHint();await loadSessions();});
listen("#repository-select","change",async()=>{localStorage.setItem("projectpilot.repository",$("#repository-select").value);selectionHint();await loadSessions();});
listen("#import-repository","click",async()=>{if(state.active)return;const url=$("#repository-url").value.trim();if(!url)throw new Error("请先输入GitHub公开仓库的链接。");const job=await post("/api/repositories/import",{url});watchJob(job);});
listen("#mode","change",async()=>{$("#mode-note").textContent=$("#mode").value==="offline"?"本地演示使用预设响应，仅验证操作流程。":"将使用已配置的模型服务处理本次问题与资料。";await loadSessions();if(state.view==="memory")await loadMemory();});
listen("#upload","change",async()=>{const file=$("#upload").files[0];if(!file)return;await api("/api/documents/"+encodeURIComponent(file.name),{method:"PUT",headers:{"Content-Type":"application/octet-stream"},body:file});$("#upload").value="";await loadDocuments();notify("资料已导入，选中后即可用于问答。");});
listen("#ask-documents","click",async()=>{if(!state.documents.size)throw new Error("请先勾选至少一份资料。");$("#engine").value="knowledge";selectionHint();await loadSessions();await navigate("chat");$("#prompt").focus();});
listen("#refresh-runs","click",loadRuns);listen("#refresh-evaluations","click",loadEvaluations);listen("#refresh-memory","click",loadMemory);listen("#memory-engine","change",loadMemory);listen("#memory-history","change",loadMemory);
listen("#select-all-cases","click",()=>[...$("#eval-cases").options].forEach(option=>option.selected=true));
listen("#eval-form","submit",async event=>{event.preventDefault();const cases=[...$("#eval-cases").selectedOptions].map(option=>option.value);const configs=$$("input[name=eval-config]:checked").map(input=>input.value);if(!cases.length||!configs.length)throw new Error("请至少选择一个任务和一种配置。");const job=await post("/api/evaluations/run",{cases,configs,repeats:Number($("#eval-repeats").value),mode:$("#mode").value});watchJob(job);notify("评测已开始，完成后会保留逐题结果。");});
document.addEventListener("click",event=>{const nav=event.target.closest("[data-view],[data-goto]");if(nav)navigate(nav.dataset.view||nav.dataset.goto).catch(error=>notify(error.message,true));const prompt=event.target.closest("[data-prompt]");if(prompt){$("#prompt").value=prompt.dataset.prompt;if(prompt.dataset.memory)$("#use-memory").checked=true;$("#prompt").focus();}});
window.addEventListener("hashchange",()=>{const view=location.hash.slice(1);if(view!==state.view)navigate(view).catch(error=>notify(error.message,true));});

(async()=>{try{state.config=await api("/api/config");$("#workspace-name").textContent=state.config.workspace_name;$("#workspace-path").textContent=state.config.workspace;$("#connection").textContent="本地已连接";if(state.config.live_enabled){const live=$("#mode option[value=live]");live.disabled=false;live.textContent="真实模型";}await loadRepositories();await loadSessions();await loadBacklog();await navigate(location.hash.slice(1)||"chat");const jobs=await api("/api/jobs");const running=jobs.find(job=>job.status==="running");if(running)watchJob(running);}catch(error){$("#connection").textContent="连接失败";notify(error.message,true);}})();
