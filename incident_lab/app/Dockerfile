# One image for every service. The service is selected at runtime by
# SERVICE_MODULE, which keeps the build cache warm across all four containers.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/ ./services/
COPY loadgen/ ./loadgen/

ENV SERVICE_MODULE=services.gateway.main:app \
    PORT=8000

CMD ["sh", "-c", "uvicorn $SERVICE_MODULE --host 0.0.0.0 --port $PORT"]
