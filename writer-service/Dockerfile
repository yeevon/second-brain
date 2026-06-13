FROM python:3.13-slim

RUN pip install --no-cache-dir \
    fastapi==0.136.3 \
    uvicorn==0.34.3 \
    pydantic==2.13.4

WORKDIR /app

COPY writer-service/src/writerservice ./writerservice

RUN groupadd --gid 10003 writerservice \
    && useradd --uid 10003 --gid 10003 --no-create-home --shell /usr/sbin/nologin writerservice \
    && mkdir -p /opt/vault \
    && chown writerservice:writerservice /opt/vault

USER 10003

EXPOSE 8001
CMD ["uvicorn", "writerservice.main:app", "--host", "0.0.0.0", "--port", "8001"]
