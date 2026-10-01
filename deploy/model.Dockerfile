# Supply an already built, immutable model image; this example does not build or modify it.
ARG MODEL_BASE
FROM ${MODEL_BASE}
USER root
ARG MODEL_SOURCE=/app/ball
RUN useradd --uid 10001 --create-home geppetto
RUN test -d "$MODEL_SOURCE" && ln -s "$MODEL_SOURCE" /opt/model
RUN pip install --no-cache-dir jsonschema==4.25.1 PyYAML==6.0.2 pydantic==2.12.0 mlflow==3.16.1
COPY geppetto /opt/geppetto/geppetto
COPY integrations /opt/geppetto/integrations
COPY contracts /opt/geppetto/contracts
# The parent image must include complete model dependencies at MODEL_SOURCE.
# Supply a reviewed datasets.json separately. No real dataset or secret is baked in here.
ENV PYTHONPATH=/opt/geppetto PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "/opt/geppetto/integrations/container_entrypoint.py"]
