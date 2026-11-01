"""Provider-native content previews, kept separate from committed answers."""

import json

from nanobot.agent.hook import AgentHook

from app.observability.trace import JsonlTrace


class AnswerPreview:
    def __init__(self, path, secrets=()):
        self.path = path
        self.secrets = tuple(secret for secret in secrets if secret)
        self.text = ""
        self.sequence = 0
        self.iteration = 0

    def publish(self, *, final_chunk=False):
        safe = self.text
        for secret in self.secrets:
            safe = safe.replace(secret, "[REDACTED]")
        if not final_chunk:
            # Hold an incomplete credential prefix across provider chunks; never
            # leak its first chunk before the complete value can be redacted.
            pending = max(
                (
                    size
                    for secret in self.secrets
                    for size in range(1, len(secret))
                    if safe.endswith(secret[:size])
                ),
                default=0,
            )
            if pending:
                safe = safe[:-pending]
        self.sequence += 1
        record = {
            "sequence": self.sequence,
            "iteration": self.iteration,
            "text": safe,
            "provisional": True,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def reset(self, iteration=None):
        if iteration is not None:
            self.iteration = iteration
        self.text = ""
        self.publish()

    def append(self, delta):
        if delta:
            self.text += delta
            self.publish()


class StreamingTrace(JsonlTrace):
    def __init__(self, directory, preview, *, secrets=(), run_id=None):
        super().__init__(directory, secrets=secrets, run_id=run_id)
        self.preview = preview

    def emit(self, event, **data):
        if event == "answer.rejected":
            self.preview.reset()
        super().emit(event, **data)


class PreviewHook(AgentHook):
    def __init__(self, preview):
        super().__init__(reraise=True)
        self.preview = preview

    def wants_streaming(self):
        return True

    async def before_iteration(self, context):
        self.preview.reset(context.iteration)

    async def on_stream(self, context, delta):
        self.preview.append(delta)

    async def on_stream_end(self, context, *, resuming):
        if resuming:
            self.preview.reset()
        else:
            self.preview.publish(final_chunk=True)


def latest_preview(path):
    if not path.is_file():
        return None
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None
