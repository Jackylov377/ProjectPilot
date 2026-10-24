"""Bounded local diagnostics and actionable messages without provider response bodies."""

import os
import re
import threading


def clean_text(text, secrets=()):
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)((?:api[_-]?key|authorization|password|secret|access_token)"
        r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]+",
        r"\1[REDACTED]",
        text,
    )
    return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)


def clean_value(value, secrets=()):
    if isinstance(value, dict):
        return {key: clean_value(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_value(item, secrets) for item in value]
    return clean_text(value, secrets) if isinstance(value, str) else value


class StderrCapture:
    """Continuously drain stderr, retaining a small tail with split secrets redacted."""

    limit = 16384

    def __init__(self, stream, secrets=()):
        self.stream = stream
        self.secrets = tuple(
            filter(None, (*secrets, os.getenv("PROJECTPILOT_API_KEY", "")))
        )
        self.tail = ""
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self):
        # Redact before trimming, so a key crossing a chunk boundary cannot leak.
        pending = ""
        discarding = False
        retain = max((1024, *(len(secret) for secret in self.secrets)))
        try:
            while chunk := self.stream.read(4096):
                if discarding:
                    if "\n" not in chunk:
                        continue
                    chunk = chunk.split("\n", 1)[1]
                    discarding = False
                pending += chunk
                if len(pending) > retain * 2:
                    # Keep any line/token spanning this boundary until it is complete.
                    boundary = pending.rfind("\n", 0, len(pending) - retain)
                    if boundary >= 0:
                        self.tail = (
                            self.tail
                            + clean_text(pending[: boundary + 1], self.secrets)
                        )[-self.limit :]
                        pending = pending[boundary + 1 :]
                    elif len(pending) > self.limit * 2 + retain:
                        # A pathological giant line is omitted rather than retaining raw fragments.
                        pending = ""
                        discarding = True
                        self.tail = (self.tail + "\n[过长的诊断行已省略]\n")[
                            -self.limit :
                        ]
            self.tail = (self.tail + clean_text(pending, self.secrets))[-self.limit :]
        except (OSError, ValueError):
            self.tail = (self.tail + "\n[诊断流读取中断]\n")[-self.limit :]
        finally:
            self.stream.close()

    def finish(self):
        self.thread.join(timeout=1)
        return self.tail


MESSAGES = {
    "github_timeout": (
        "连接 GitHub 或下载源码超时。",
        "请检查网络后重试。已经导入的仓库仍可继续分析。",
    ),
    "github_connection": (
        "获取 GitHub 源码失败。",
        "请检查网络和仓库链接；当前仅支持公开仓库的默认分支。",
    ),
    "record_invalid": (
        "这条任务记录损坏或无法读取。",
        "原始文件已保留；可在本地数据目录按任务编号检查 job.json，其他任务可继续使用。",
    ),
    "result_invalid": (
        "任务结果文件损坏或格式不完整。",
        "本次结果不能视为成功；请查看本地诊断并重新提交任务。",
    ),
    "start_failed": (
        "任务进程未能启动。",
        "请检查 Python 环境和本地目录权限，然后重新提交。",
    ),
    "timeout": (
        "任务达到时间上限。",
        "可以缩小问题范围或减少选中的资料，再检查模型服务是否正常。",
    ),
    "authentication": (
        "模型服务拒绝了身份验证或访问权限。",
        "请检查本地 .env 中的密钥及账号权限，修改后重启服务。",
    ),
    "rate_limit": (
        "模型服务暂时限制了请求。",
        "请检查账号额度或请求频率，稍后再手动重试。",
    ),
    "model_or_endpoint": (
        "未找到配置的模型或接口地址。",
        "请核对 .env 中的模型标识和兼容 API 地址，修改后重启服务。",
    ),
    "connection": (
        "无法稳定连接模型服务。",
        "请检查网络和 API 地址，恢复连接后重新提交。",
    ),
    "server_error": (
        "模型服务暂时发生错误。",
        "请稍后重试；持续失败时检查服务商状态。",
    ),
    "provider_request": (
        "模型服务拒绝了本次请求。",
        "请检查模型是否支持工具调用，以及接口地址和请求参数是否兼容。",
    ),
    "configuration": (
        "运行配置不完整或无效。",
        "请检查本地模型配置及所选资料，修正后重启服务或重新提交。",
    ),
    "context_budget": (
        "本次请求超过上下文预算。",
        "请新建会话、缩短问题或减少选中的资料。",
    ),
    "answer_validation": (
        "回答纠正后仍未通过引用或格式检查。",
        "本次预览不能作为确认结果；请缩小问题范围后重试。",
    ),
    "max_iterations": (
        "任务达到工具调用轮数上限。",
        "请将问题拆小，再针对一部分源码或资料提问。",
    ),
    "permission": (
        "本地文件无法读取或保存。",
        "请检查工作目录是否存在，以及当前用户是否有读写权限。",
    ),
    "process_error": (
        "任务未正常完成。",
        "已保留本地诊断；请按任务编号检查 diagnostic.json 后再重试。",
    ),
}


def failure_details(*, stop_reason="", error_kind="", status_code=None, exception=None):
    code = "process_error"
    if stop_reason in MESSAGES:
        code = stop_reason
    elif status_code in (401, 403):
        code = "authentication"
    elif status_code == 429:
        code = "rate_limit"
    elif status_code == 404:
        code = "model_or_endpoint"
    elif isinstance(status_code, int) and status_code >= 500:
        code = "server_error"
    elif isinstance(status_code, int) and status_code >= 400:
        code = "provider_request"
    elif error_kind in MESSAGES:
        code = error_kind
    elif isinstance(exception, TimeoutError):
        code = "timeout"
    elif isinstance(exception, (PermissionError, FileNotFoundError)):
        code = "permission"
    elif isinstance(exception, ValueError):
        code = "configuration"
    message, hint = MESSAGES[code]
    return {"error_code": code, "error": message, "error_hint": hint}
