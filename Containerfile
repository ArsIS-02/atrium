FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV FLASK_APP=app.main \
    HOMELAB_DIR=/data \
    CADDY_IMPORT=/caddy/Caddyfile.apps \
    STATUS_INTERVAL=30 \
    ZONE=lab.example
EXPOSE 5000
CMD ["python", "-m", "flask", "run", "--host=0.0.0.0", "--port=5000"]
