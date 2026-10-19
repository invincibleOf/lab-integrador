FROM python:3.12.7-slim

WORKDIR /app

# Sin apt-get: psycopg2-binary ya trae libpq compilada; gcc + libpq-dev
# solo agregaban ~110 MB a la imagen sin usarse.

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV SERVICE_NAME=data-service
ENV APP_VERSION=1.0.0
ENV ENVIRONMENT=production

EXPOSE 8002

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
