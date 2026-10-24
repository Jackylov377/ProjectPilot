import { api, post } from "./api.js";
import { isCurrentConversation, loadSessions } from "./chat.js";
import { loadEvaluations } from "./evaluations.js";
import { loadRuns } from "./runs.js";
import { state } from "./state.js";
import { $, $$, handleAction, node, notify, progressLabel, setBusy, statusTitles } from "./ui.js";
import { loadRepositories } from "./workspace.js";

function startMessage(job) {
  if (job.kind === "evaluation") return "评测已开始，可在运行记录中查看状态。";
  if (job.kind === "repository_import") return "正在从GitHub获取源码，请稍候。";
  return "任务已提交，正在准备。";
}

function showAnswerPreview(job, preview) {
  if (!isCurrentConversation(job)) return;
  const messages = $("#messages");
  let article = messages.querySelector("[data-preview]");
  if (!preview.text) {
    if (article) article.remove();
    return;
  }
  if (!article) {
    article = node("article", undefined, "message assistant preview-message");
    article.dataset.preview = job.id;
    article.append(
      node("span", "ProjectPilot · 生成预览，以最终保存结果为准", "message-label"),
      node("div", "", "message-body"),
    );
    messages.append(article);
  }
  article.querySelector(".message-body").textContent = preview.text;
  messages.scrollTop = messages.scrollHeight;
}

async function refreshFinishedJob(job) {
  if (job.kind === "repository_import" && job.status === "completed") {
    $("#engine").value = "repository";
    await loadRepositories(job.id);
    $("#session").value = "repo-" + job.id.slice(0, 8);
    await loadSessions();
    notify("源码已导入，当前已选中该仓库。输入问题即可分析。");
  }
  if (isCurrentConversation(job)) {
    await loadSessions();
    if (job.result?.reports?.length) notify("报告已保存，可在运行详情中查看正文。");
  }
  if (state.view === "runs") await loadRuns();
  if (job.kind === "evaluation" && state.view === "evaluations") await loadEvaluations();

  if (job.status !== "completed") {
    notify(job.error || "本次运行未完成，记录已保留。", true);
  } else if (job.kind === "evaluation") {
    notify("评测执行完成，具体通过情况请查看报告。");
  }
}

function finishJob(stream, job) {
  stream.close();
  state.events = null;
  state.active = null;
  setBusy(false);

  // 生成预览可能被模型纠正；结束后重新读取已保存的历史作为最终结果。
  $$("#messages [data-preview]").forEach((preview) => preview.remove());
  $("#progress-text").textContent = job.status === "completed"
    ? "运行已结束，结果已保存。"
    : job.error || statusTitles[job.status];
  return refreshFinishedJob(job);
}

export function watchJob(job) {
  // 所有任务共享一个订阅，页面刷新后也通过已有运行记录恢复跟踪。
  if (state.events) state.events.close();
  state.active = job.id;
  setBusy(true);
  $("#progress-text").textContent = startMessage(job);

  const stream = new EventSource("/api/jobs/" + job.id + "/events");
  state.events = stream;
  stream.addEventListener("progress", (event) => {
    $("#progress-text").textContent = progressLabel(JSON.parse(event.data));
  });
  stream.addEventListener("answer", (event) => {
    showAnswerPreview(job, JSON.parse(event.data));
  });
  stream.addEventListener("done", handleAction((event) => {
    return finishJob(stream, JSON.parse(event.data));
  }));
  stream.onerror = () => {
    $("#progress-text").textContent = "正在重新连接进度，任务状态仍会保留。";
  };
}

export async function restoreRunningJob() {
  const jobs = await api("/api/jobs");
  const running = jobs.find((job) => job.status === "running");
  if (running) watchJob(running);
}

export async function cancelActiveJob() {
  if (state.active) await post("/api/jobs/" + state.active + "/cancel");
}

export async function importRepository() {
  if (state.active) return;
  const url = $("#repository-url").value.trim();
  if (!url) throw new Error("请先输入GitHub公开仓库的链接。");
  const job = await post("/api/repositories/import", { url });
  watchJob(job);
}
