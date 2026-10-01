ARG MODEL_BASE
FROM ${MODEL_BASE}
USER root
COPY geppetto /opt/geppetto/geppetto
COPY integrations /opt/geppetto/integrations
COPY contracts /opt/geppetto/contracts
ENV PYTHONPATH=/opt/geppetto PYTHONDONTWRITEBYTECODE=1
USER 10001
ENTRYPOINT ["python", "/opt/geppetto/integrations/container_entrypoint.py"]
