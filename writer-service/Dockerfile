FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    gosu \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    fastapi==0.136.3 \
    uvicorn==0.34.3 \
    pydantic==2.13.4 \
    httpx>=0.28.1

WORKDIR /app

COPY writer-service/src/writerservice ./writerservice
COPY writer-service/docker-entrypoint.sh /usr/local/bin/writer-entrypoint
RUN chmod +x /usr/local/bin/writer-entrypoint

RUN mkdir -p /opt/vault

ENTRYPOINT ["/usr/local/bin/writer-entrypoint"]
CMD ["uvicorn", "writerservice.main:app", "--host", "0.0.0.0", "--port", "8001"]
