FROM python:3.13-slim

RUN pip install --no-cache-dir fastapi==0.136.3 uvicorn==0.34.3 httpx==0.28.1

WORKDIR /app
COPY app.py .

RUN useradd --uid 10002 --no-create-home --shell /bin/false writer-stub
USER 10002

EXPOSE 8001
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
