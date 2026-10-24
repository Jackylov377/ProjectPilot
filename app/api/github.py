"""Import a public GitHub default-branch snapshot without executing repository code."""

import re
import stat
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

import httpx

from app.api.diagnostics import failure_details
from app.api.jobs import read, write
from app.observability.trace import JsonlTrace
from app.tools.list_files import EXCLUDED_NAMES

MAX_DOWNLOAD = 20 * 1024 * 1024
MAX_EXTRACTED = 60 * 1024 * 1024
MAX_FILES = 8000


def github_repository(url):
    parsed = urlsplit(url.strip())
    parts = parsed.path.rstrip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.query
        or parsed.fragment
        or len(parts) != 3
        or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", parts[1])
    ):
        raise ValueError("请输入公开仓库首页链接，例如 https://github.com/owner/repo。")
    owner, name = parts[1], parts[2].removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name) or name in {".", ".."}:
        raise ValueError("GitHub仓库名称格式不正确。")
    return owner, name, f"https://github.com/{owner}/{name}"


def unpack(archive, destination):
    files, total, skipped = 0, 0, 0
    with ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if (
            len(entries) > MAX_FILES
            or sum(item.file_size for item in entries) > MAX_EXTRACTED
        ):
            raise ValueError("仓库解压后过大，请选择较小的仓库。")
        roots = set()
        for item in entries:
            path = PurePosixPath(item.filename)
            if (
                path.is_absolute()
                or "\\" in item.filename
                or ".." in path.parts
                or any(":" in part for part in path.parts)
                or not path.parts
            ):
                raise ValueError("源码压缩包包含不合法的文件路径。")
            roots.add(path.parts[0])
        if len(roots) != 1:
            raise ValueError("源码压缩包目录结构不符合GitHub快照格式。")
        destination.mkdir(exist_ok=False)
        for item in entries:
            relative = PurePosixPath(item.filename).parts[1:]
            if item.is_dir() or not relative:
                continue
            if stat.S_ISLNK(item.external_attr >> 16) or any(
                part.casefold() in EXCLUDED_NAMES or part.casefold().startswith(".env")
                for part in relative
            ):
                skipped += 1
                continue
            target = destination.joinpath(*relative)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("源码路径超出导入目录。")
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(item) as source, target.open("xb") as output:
                while block := source.read(65536):
                    total += len(block)
                    if total > MAX_EXTRACTED:
                        raise ValueError("仓库解压后超过大小上限。")
                    output.write(block)
            files += 1
        comment = bundle.comment.decode("ascii", errors="ignore").strip()
    if not files:
        raise ValueError("未取得可分析的源码文件。")
    return {
        "files": files,
        "skipped": skipped,
        "extracted_bytes": total,
        "commit": comment if re.fullmatch(r"[0-9a-f]{40}", comment) else None,
    }


def import_repository(url, directory, trace):
    owner, name, canonical = github_repository(url)
    trace.emit("repository.import.started", repository=canonical)
    archive = directory / "source.zip"
    downloaded = 0
    fingerprint = sha256()
    # Fixed host and HTTPS; arbitrary URLs and redirects never become fetch targets.
    with httpx.stream(
        "GET",
        f"https://codeload.github.com/{owner}/{name}/zip/HEAD",
        timeout=20,
        follow_redirects=False,
        headers={"User-Agent": "ProjectPilot/0.1"},
    ) as response:
        if response.status_code == 404:
            raise ValueError("仓库不存在或不是公开仓库，请检查链接。")
        if response.status_code != 200:
            raise ValueError(
                f"GitHub下载失败（HTTP {response.status_code}），请稍后重试。"
            )
        with archive.open("xb") as output:
            for block in response.iter_bytes(65536):
                downloaded += len(block)
                if downloaded > MAX_DOWNLOAD:
                    raise ValueError("源码压缩包超过20 MB，请选择较小的仓库。")
                fingerprint.update(block)
                output.write(block)
    trace.emit("repository.downloaded", bytes=downloaded)
    details = unpack(archive, directory / "source")
    result = {
        "id": directory.name,
        "name": f"{owner}/{name}",
        "url": canonical,
        "ref": "HEAD",
        "workspace": str((directory / "source").resolve()),
        "archive_sha256": fingerprint.hexdigest(),
        "downloaded_bytes": downloaded,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        **details,
    }
    trace.emit("repository.ready", repository=canonical, **details)
    return result


def main():
    directory = Path(sys.argv[1]).resolve()
    trace = JsonlTrace(directory / "traces")
    try:
        repository = import_repository(
            read(directory / "job.json")["request"]["url"], directory, trace
        )
        result = {"stop_reason": "completed", "repository": repository}
    except (ValueError, OSError, httpx.HTTPError, BadZipFile) as exc:
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            else "获取源码失败，请检查网络或更换仓库后重试。"
        )
        trace.emit(
            "repository.import.failed", error=type(exc).__name__, message=message
        )
        details = failure_details(
            stop_reason=(
                "github_timeout"
                if isinstance(exc, httpx.TimeoutException)
                else "github_connection"
            )
        )
        result = {"stop_reason": "error", **details, "error": message}
        if isinstance(exc, httpx.TimeoutException):
            result["error"] = details["error"]
    write(directory / "result.json", result)


if __name__ == "__main__":
    main()
