"""Project document originals, optimistic knowledge settings and memory management."""

import hashlib
from typing import Literal
from urllib.parse import quote

from fastapi import Query, Request
from fastapi.responses import Response
from pydantic import Field, StrictInt, StrictStr
from starlette.concurrency import run_in_threadpool

from app.workspace.documents import MAX_UPLOAD, content_path
from app.workspace.errors import Problem
from .workspace import Input


class KnowledgeSettings(Input):
    request_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    expected_revision: StrictInt = Field(ge=0)
    document_ids: list[StrictStr] = Field(max_length=20)
    retriever: Literal["bm25", "vector", "hybrid", "hybrid_rerank"]


class RevokeMemory(Input):
    request_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    environment: Literal["offline", "live"]
    expected_revision: StrictInt = Field(ge=1)


async def upload_bytes(request):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_UPLOAD:
            raise Problem("document_size", "单份资料最多 4 MiB。", 413)
    return bytes(content)


def install_resource_api(app, access, cleaner):
    @app.get("/api/projects/{project_id}/documents")
    def documents(project_id: str):
        return cleaner.clean(access.require().documents.list(project_id))

    @app.post("/api/projects/{project_id}/documents", status_code=201)
    async def publish(project_id: str, request: Request, name: str = Query(min_length=1, max_length=104),
                      request_id: str = Query(pattern=r"^[A-Za-z0-9_-]{1,64}$")):
        service = access.require()
        content = await upload_bytes(request)
        result = await run_in_threadpool(service.documents.publish, project_id, request_id=request_id, content=content, name=name)
        return cleaner.clean(result)

    @app.get("/api/projects/{project_id}/documents/{document_id}")
    def document(project_id: str, document_id: str):
        return cleaner.clean(access.require().documents.document(project_id, document_id))

    @app.get("/api/projects/{project_id}/documents/{document_id}/versions")
    def versions(project_id: str, document_id: str):
        return cleaner.clean(access.require().documents.versions(project_id, document_id))

    @app.post("/api/projects/{project_id}/documents/{document_id}/versions", status_code=201)
    async def revise(project_id: str, document_id: str, request: Request,
                     request_id: str = Query(pattern=r"^[A-Za-z0-9_-]{1,64}$"),
                     expected_revision: int = Query(ge=1)):
        service = access.require()
        content = await upload_bytes(request)
        result = await run_in_threadpool(service.documents.publish, project_id, request_id=request_id,
                                        content=content, document_id=document_id, expected_revision=expected_revision)
        return cleaner.clean(result)

    @app.get("/api/projects/{project_id}/documents/{document_id}/versions/{version_id}")
    def version(project_id: str, document_id: str, version_id: str):
        return cleaner.clean(access.require().documents.version(project_id, document_id, version_id))

    @app.get("/api/projects/{project_id}/documents/{document_id}/versions/{version_id}/raw")
    def original(project_id: str, document_id: str, version_id: str):
        service = access.require()
        version = service.documents.version(project_id, document_id, version_id)
        try:
            with content_path(service.data_dir, version["locator"]).open("rb") as handle:
                raw = handle.read(MAX_UPLOAD + 1)
            if len(raw) > MAX_UPLOAD or hashlib.sha256(raw).hexdigest() != version["content_sha256"]:
                raise ValueError("Content mismatch")
        except (OSError, ValueError):
            raise Problem("document_unavailable", "原版本已缺失或损坏，未替换为当前版本。") from None
        return Response(raw, media_type="application/pdf" if version["media_type"] == ".pdf" else "text/plain",
                        headers={"Content-Disposition": "inline; filename*=UTF-8''" + quote(version["name"], safe="")})

    @app.get("/api/tasks/{task_id}/knowledge-settings")
    def settings(task_id: str):
        return access.require().documents.settings(task_id)

    @app.put("/api/tasks/{task_id}/knowledge-settings")
    def select(task_id: str, body: KnowledgeSettings):
        return access.require().documents.select(task_id, **body.model_dump())

    @app.get("/api/projects/{project_id}/memories")
    def memories(project_id: str, environment: Literal["offline", "live"], history: bool = False):
        return cleaner.clean(access.require().memories.list(project_id, environment, history=history))

    @app.post("/api/projects/{project_id}/memories/{memory_id}/revoke")
    def revoke(project_id: str, memory_id: str, body: RevokeMemory):
        return cleaner.clean(access.require().memories.revoke(project_id, memory_id, **body.model_dump()))
