FROM python:3.12-slim AS sdk-builder
WORKDIR /src
COPY sdk ./sdk
RUN pip wheel --no-deps --wheel-dir /dist ./sdk

FROM python:3.12-slim
WORKDIR /app
COPY requirements-runtime.lock ./
RUN pip install --no-cache-dir -r requirements-runtime.lock
COPY geppetto ./geppetto
COPY contracts ./contracts
COPY --from=sdk-builder /dist/geppetto_sdk-*.whl ./dist/
COPY docs/PYTHON_SDK.md ./docs/PYTHON_SDK.md
COPY docs/PLATFORM_0_4.md docs/PLATFORM_0_2.md docs/PLATFORM_0_3.md docs/ACCOUNTS.md docs/RUN_HISTORY.md docs/DETECTION_VISUALS.md docs/GRADCAM.md ./docs/
COPY web ./web
COPY deploy/agent.example.yaml ./deploy/agent.example.yaml
COPY deploy/agent-execution.example.yaml deploy/agent-execution.patch.yaml ./deploy/
RUN useradd --uid 10001 --create-home geppetto && mkdir -p /state && chown 10001 /state
USER 10001
ENV GEPPETTO_HOME=/state GEPPETTO_MODE=catalog PYTHONDONTWRITEBYTECODE=1
EXPOSE 8091
CMD ["python", "-m", "geppetto.app", "--host", "0.0.0.0", "--port", "8091"]
