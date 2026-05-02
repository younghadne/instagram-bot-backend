FROM python:3.11-slim

WORKDIR /app

# Support both requirements.txt and requirements-web.txt filenames
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || pip install --no-cache-dir -r requirements-web.txt

COPY . .

RUN mkdir -p sessions

EXPOSE 10000

# Use shell form so $PORT env var is expanded at runtime
CMD sh -c "gunicorn --worker-class gthread -w 1 --threads 100 --bind 0.0.0.0:${PORT:-10000} --timeout 120 --access-logfile - --error-logfile - --capture-output web_app:app"
