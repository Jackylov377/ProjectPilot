"""Phase-two metadata additions; no legacy conversation or memory migration."""

SERVICE_SCHEMA = """
CREATE TABLE project_bindings (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    snapshot_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE task_runtime (
    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
    environment TEXT NOT NULL CHECK(environment IN ('offline','live')),
    bound_at TEXT NOT NULL
);
CREATE TRIGGER fixed_environment BEFORE UPDATE ON task_runtime
BEGIN SELECT RAISE(ABORT,'immutable_field'); END;
CREATE TABLE request_receipts (
    operation TEXT NOT NULL, request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL, result_id TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY(operation,request_id)
);
CREATE UNIQUE INDEX task_project_pair ON tasks(task_id,project_id);
CREATE TABLE task_runs (
    run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, project_id TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE, envelope_json TEXT NOT NULL,
    admission_state TEXT NOT NULL CHECK(admission_state IN ('accepted','interrupted')),
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id,project_id) REFERENCES tasks(task_id,project_id)
);
CREATE TRIGGER fixed_run BEFORE UPDATE OF run_id,task_id,project_id,request_id,envelope_json ON task_runs
BEGIN SELECT RAISE(ABORT,'immutable_field'); END;
"""

RESOURCE_SCHEMA = """
CREATE TABLE documents (
    document_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
    name TEXT NOT NULL, name_key TEXT NOT NULL, media_type TEXT NOT NULL,
    current_version_id TEXT, revision INTEGER NOT NULL CHECK(revision>=0),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(project_id,document_id), UNIQUE(project_id,name_key),
    FOREIGN KEY(project_id,document_id,current_version_id)
        REFERENCES document_versions(project_id,document_id,version_id)
);
CREATE TABLE document_versions (
    version_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, document_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision>0), content_sha256 TEXT NOT NULL,
    byte_count INTEGER NOT NULL, locator TEXT NOT NULL, preview_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(document_id,revision), UNIQUE(project_id,document_id,version_id),
    FOREIGN KEY(project_id,document_id) REFERENCES documents(project_id,document_id)
);
CREATE TRIGGER fixed_document_version BEFORE UPDATE ON document_versions
BEGIN SELECT RAISE(ABORT,'immutable_field'); END;
CREATE TRIGGER fixed_document_identity BEFORE UPDATE ON documents
WHEN NEW.document_id IS NOT OLD.document_id OR NEW.project_id IS NOT OLD.project_id OR
     NEW.name IS NOT OLD.name OR NEW.name_key IS NOT OLD.name_key OR NEW.media_type IS NOT OLD.media_type
BEGIN SELECT RAISE(ABORT,'immutable_field'); END;
CREATE TABLE task_knowledge (
    task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
    retriever TEXT NOT NULL CHECK(retriever IN ('bm25','vector','hybrid','hybrid_rerank')),
    revision INTEGER NOT NULL CHECK(revision>=0),
    FOREIGN KEY(task_id,project_id) REFERENCES tasks(task_id,project_id)
);
CREATE TABLE task_documents (
    task_id TEXT NOT NULL, project_id TEXT NOT NULL, document_id TEXT NOT NULL,
    PRIMARY KEY(task_id,document_id),
    FOREIGN KEY(task_id,project_id) REFERENCES tasks(task_id,project_id),
    FOREIGN KEY(project_id,document_id) REFERENCES documents(project_id,document_id)
);
CREATE UNIQUE INDEX run_project_pair ON task_runs(run_id,project_id);
CREATE TABLE run_documents (
    run_id TEXT NOT NULL, project_id TEXT NOT NULL,
    document_id TEXT NOT NULL, version_id TEXT NOT NULL,
    PRIMARY KEY(run_id,document_id),
    FOREIGN KEY(run_id,project_id) REFERENCES task_runs(run_id,project_id),
    FOREIGN KEY(project_id,document_id,version_id)
        REFERENCES document_versions(project_id,document_id,version_id)
);
CREATE TRIGGER fixed_run_document BEFORE UPDATE ON run_documents
BEGIN SELECT RAISE(ABORT,'immutable_field'); END;
INSERT INTO task_knowledge SELECT task_id,project_id,'bm25',0 FROM tasks WHERE task_mode='knowledge';
"""
