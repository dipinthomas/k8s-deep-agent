FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY topology.py distributions.py synth.py backfill.py ./

# Default to in-cluster behaviour: ship to the sidecar collector and publish
# CloudWatch metrics. Local runs override these to ConsoleSpanExporter / no-CW.
ENV OTLP_ENDPOINT=http://localhost:4317 \
    ENABLE_CW=true \
    SCENARIO=healthy \
    RPS=15 \
    WORKERS=32

CMD ["python", "synth.py"]
