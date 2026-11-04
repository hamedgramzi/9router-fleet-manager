FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY panel ./panel
COPY nginx ./nginx

ENV FLEET_BASE_DIR=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 21000

CMD ["uvicorn", "panel.server:app", "--host", "0.0.0.0", "--port", "21000"]
