FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY licenses ./licenses
COPY app ./app
COPY examples/demo_project ./examples/demo_project
COPY evals/datasets/agent_dev/tasks_v2.json ./evals/datasets/agent_dev/tasks_v2.json
COPY scripts/prepare_tokenizer.py ./scripts/prepare_tokenizer.py
RUN pip install --no-cache-dir .
RUN python scripts/prepare_tokenizer.py
EXPOSE 8765
CMD ["python", "-m", "app.api", "--host", "0.0.0.0", "--port", "8765"]
