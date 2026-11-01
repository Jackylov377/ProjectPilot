import {state, checkpoint, emit} from './state.js?v=workspace-4-2';
import { api } from "./api.js?v=workspace-4-2";
import { showGroups } from "./evaluations.js?v=workspace-4-2";
import { $, badge, date, empty, handleAction, node, progressLabel, action } from "./ui.js?v=workspace-4-2";

const timelineEvents = new Set([
  "run.started",
  "tool.started",
  "tool.finished",
  "context.summary.saved",
  "memory.recalled",
  "knowledge.retrieval_fallback",
  "knowledge.retrieval_completed",
  "answer.finalizing",
  "run.finished",
  "repository.import.started",
  "repository.downloaded",
  "repository.ready",
  "repository.import.failed",
]);

function runTitle(job) {
  if (job.kind === "invalid") return "任务记录损坏 · " + job.id.slice(0, 8);
  if (job.kind === "evaluation") return "开发评测";
  if (job.kind === "repository_import") return "获取GitHub源码 · " + job.request.url;
  return job.request.prompt || "历史任务";
}

function runContext(job) {
  if (job.kind === "evaluation") return "固定任务集";
  if (job.kind === "repository_import") return "仓库导入";
  const project=state.projects.find(p => p.project_id === job.request.project_id);
  const task=(state.tasks.get(job.request.project_id) || []).find(t => t.task_id === job.request.task_id);
  return project ? project.name + ' / ' + (task?.name || '任务') : job.request.task_id ? '所属项目或任务不可用' : '改造前运行 · ' + (job.request.session || job.id);
}

function detailTitle(job) {
  if (job.kind === "invalid") return "任务记录损坏";
  if (job.kind === "agent") return "任务与结果";
  if (job.kind === "repository_import") return "源码导入";
  return "评测执行";
}

function requestDescription(job) {
  if (job.request.prompt) return job.request.prompt;
  if (job.request.url) return job.request.url;
  if (job.request.configs) return "配置：" + job.request.configs.join(" / ");
  return "任务编号：" + job.id;
}

let listSequence=0, detailSequence=0;
export async function loadRuns() {
  const current=checkpoint(), sequence=++listSequence; ++detailSequence;
  const jobs = await api("/api/jobs");
  if (!current() || sequence !== listSequence) return;
  const container = $("#runs-list");
  container.replaceChildren();
  if (!jobs.length) empty(container, "还没有运行记录。发送一条问题即可开始。");

  for (const job of jobs) {
    const row = node("div", undefined, "list-item selectable");
    const info = node("div");
    const timestamp = job.created_at ? date(job.created_at) : "原始记录已保留";
    info.append(node("strong", runTitle(job).slice(0, 65)), node("p", timestamp + " · " + runContext(job)));
    row.append(info, badge(job.status));
    row.onclick = handleAction(() => showRun(job.id));
    container.append(row);
  }
}

function appendRepository(container, repository) {
  container.append(node("pre", repository.name
    + "\n默认分支快照 · " + repository.files + " 个文件"
    + "\n提交：" + (repository.commit || "未提供提交标识")
    + "\n" + repository.url));
}

function appendSavedResult(container, job) {
  if (job.error) container.append(node("pre", job.error));
  if (job.error_hint) container.append(node("p", job.error_hint));
  if (job.persistence_warning) container.append(node("p", job.persistence_warning));
  if (job.error_code) {
    container.append(node("small", "排查编号：" + job.id + " · " + job.error_code));
  }
  if (job.result?.answer) container.append(node("pre", job.result.answer));
  for (const warning of job.result?.retrieval_warnings || []) {
    container.append(node("p", warning));
  }
  for (const report of job.result?.reports || []) {
    const detail = node("details");
    detail.append(node("summary", "生成报告 · " + report.name), node("pre", report.text));
    container.append(detail);
  }
  if (job.kind === "evaluation" && job.result?.summary) {
    showGroups(container, job.result.summary.groups);
  }
}

function appendTrace(container, records) {
  if (!records.length) return;
  container.append(node("h4", "执行过程"));
  const timeline = node("ul", undefined, "timeline");
  for (const record of records.filter((item) => timelineEvents.has(item.event))) {
    const row = node("li", progressLabel(record));
    row.append(node("span", (record.elapsed_ms / 1000).toFixed(2) + " 秒"));
    timeline.append(row);
  }
  container.append(timeline);
  const raw = node("details");
  raw.append(
    node("summary", "查看详细过程数据"),
    node("pre", JSON.stringify(records, null, 2)),
  );
  container.append(raw);
}

async function showRun(id) {
  const current=checkpoint(), sequence=++detailSequence;
  const job = await api("/api/jobs/" + id);
  if (!current() || sequence !== detailSequence) return;
  const container = $("#run-detail");
  container.replaceChildren();
  container.classList.remove("empty-copy");
  container.append(badge(job.status), node("h4", detailTitle(job)), node("pre", requestDescription(job)));
  if (job.result?.repository) appendRepository(container, job.result.repository);

  if (job.status !== "running") {
    const download = node("a", "导出本次结果（ZIP）", "text-button");
    download.href = "/api/jobs/" + job.id + "/export";
    download.setAttribute("download", "");
    container.append(download);
  }
  if (job.request?.task_id) container.append(action('打开所属任务',() => emit('ui:navigate',{view:'chat',projectId:job.request.project_id,taskId:job.request.task_id})));
  appendSavedResult(container, job);
  const records = await api("/api/jobs/" + id + "/trace");
  if (current() && sequence === detailSequence) appendTrace(container, records);
}
