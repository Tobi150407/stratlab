FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt && useradd --system --uid 10001 strategylab

COPY app ./app
USER strategylab

# Each service overrides CMD (see docker-compose.yml). Default = API.
EXPOSE 8000
CMD ["python", "-m", "app.cli", "api"]
