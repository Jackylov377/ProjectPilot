"""Phase-two task API, kept separate from the legacy workbench UI."""

from pathlib import Path
import threading
from typing import Literal

from fastapi import Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from app.workspace import (
    ImmutableField, InvalidConfiguration, NameConflict, NotFound, StorageError,
    UnsupportedVersion, WorkspaceStore,
)
from app.workspace.history import LegacyHistory, task_history
from app.workspace.service import Problem, WorkspaceService


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(Input):
    request_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: StrictStr = Field(min_length=1, max_length=200)
    source_kind: Literal["local", "github", "collection"]
    repository_id: StrictStr | None = Field(default=None, pattern=r"^(startup|[0-9a-f]{32})$")
    create_independent: StrictBool = False


class TaskCreate(Input):
    request_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: StrictStr = Field(min_length=1, max_length=200)
    task_mode: Literal["repository", "knowledge"]
    memory_enabled: StrictBool
    context_policy: Literal["window", "full", "summary"]


class Rename(Input):
    name: StrictStr = Field(min_length=1, max_length=200)


class Binding(Input):
    repository_id: StrictStr = Field(pattern=r"^(startup|[0-9a-f]{32})$")


class RunCreate(Input):
    request_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    prompt: StrictStr = Field(min_length=1, max_length=12000)
    environment: Literal["offline", "live"] | None = None


class WorkspaceAccess:
    def __init__(self, manager, workspace, data_dir, live_enabled):
        self.manager, self.workspace, self.data_dir = manager, Path(workspace), Path(data_dir)
        self.live_enabled, self.service = live_enabled, None
        self.lock = threading.RLock()

    def get(self, create=False):
        with self.lock:
            if self.service is None:
                path = self.data_dir / "workspace.sqlite3"
                if not path.exists() and not create:
                    return None
                store = WorkspaceStore.upgrade(path) if path.exists() else WorkspaceStore.initialize(path)
                self.service = WorkspaceService(store, self.manager, self.workspace, self.data_dir,
                                                live_enabled=self.live_enabled)
            return self.service

    def require(self):
        service = self.get()
        if service is None:
            raise Problem("not_found", "未找到项目或任务。", 404)
        return service


def install_workspace_api(app, manager, workspace, data_dir, live_enabled, cleaner):
    access = WorkspaceAccess(manager, workspace, data_dir, live_enabled)
    access.get()  # Recover existing admissions, but never create a DB for legacy readers.
    app.state.workspace = access
    legacy = LegacyHistory(data_dir, workspace)

    @app.exception_handler(Problem)
    async def problem(_request, exc):
        return JSONResponse({"detail": exc.message, "error_code": exc.code}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, exc):
        return JSONResponse({"detail": "请求参数缺失、类型不正确或包含不允许的字段。",
                             "error_code": "invalid_request",
                             "errors": [{key: error[key] for key in ("type", "loc", "msg") if key in error}
                                        for error in exc.errors()]}, status_code=422)

    errors = {
        NameConflict: (409, "name_conflict", "此范围内已存在同名条目。"),
        NotFound: (404, "not_found", "未找到项目或任务。"),
        InvalidConfiguration: (422, "invalid_configuration", "名称、配置或归属不符合要求。"),
        ImmutableField: (409, "immutable_field", "任务配置与项目来源不可修改。"),
        UnsupportedVersion: (503, "unsupported_version", "元数据版本不受支持，未重建数据。"),
        StorageError: (503, "storage_unavailable", "元数据存储不可用，未清空数据。"),
    }

    def make_error_handler(status, code, message):
        async def handler(_request, _exc):
            return JSONResponse({"detail": message, "error_code": code}, status_code=status)
        return handler

    for error, details in errors.items():
        app.add_exception_handler(error, make_error_handler(*details))

    @app.get("/api/projects")
    def projects(source_kind: Literal["local", "github", "collection"] | None = None,
                 repository_id: str | None = Query(default=None, pattern=r"^(startup|[0-9a-f]{32})$")):
        service = access.get()
        if service is None:
            return []
        key = service.source(source_kind, repository_id)["source_key"] if source_kind in ("local", "github") else None
        found = service.store.list_projects(source_key=key)
        if source_kind == "collection":
            found = [project for project in found if project.source_kind == "collection"]
        return cleaner.clean([service.project(project.project_id) for project in found])

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectCreate):
        return cleaner.clean(access.get(create=True).create_project(**body.model_dump()))

    @app.get("/api/projects/{project_id}")
    def project(project_id: str):
        return cleaner.clean(access.require().project(project_id))

    @app.patch("/api/projects/{project_id}")
    def rename_project(project_id: str, body: Rename):
        service = access.require()
        service.store.rename_project(project_id, body.name)
        return cleaner.clean(service.project(project_id))

    @app.put("/api/projects/{project_id}/repository-binding")
    def bind(project_id: str, body: Binding):
        return cleaner.clean(access.require().bind_repository(project_id, body.repository_id))

    @app.get("/api/projects/{project_id}/tasks")
    def tasks(project_id: str):
        service = access.require()
        return cleaner.clean([service.task(task.task_id) for task in service.store.list_tasks(project_id)])

    @app.post("/api/projects/{project_id}/tasks", status_code=201)
    def create_task(project_id: str, body: TaskCreate):
        return cleaner.clean(access.require().create_task(project_id, **body.model_dump()))

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str):
        return cleaner.clean(access.require().task(task_id))

    @app.patch("/api/tasks/{task_id}")
    def rename_task(task_id: str, body: Rename):
        service = access.require()
        service.store.rename_task(task_id, body.name)
        return cleaner.clean(service.task(task_id))

    @app.post("/api/tasks/{task_id}/runs", status_code=202)
    def submit(task_id: str, body: RunCreate):
        return cleaner.clean(access.require().submit(task_id, **body.model_dump()))

    @app.get("/api/tasks/{task_id}/runs")
    def runs(task_id: str):
        return cleaner.clean(access.require().runs(task_id))

    @app.get("/api/tasks/{task_id}/history")
    def history(task_id: str):
        return cleaner.clean(task_history(access.require(), task_id))

    @app.get("/api/legacy/histories")
    def legacy_list():
        return cleaner.clean(legacy.list())

    @app.get("/api/legacy/histories/{legacy_id}")
    def legacy_detail(legacy_id: str):
        return cleaner.clean(legacy.read(legacy_id))

    from .resources import install_resource_api
    install_resource_api(app, access, cleaner)
    return access, legacy
