ARG PYTHON_BASE_IMAGE=python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEMO_TARGET_HTTP_PORT=8080 \
    DEMO_TARGET_GRPC_PORT=50051

WORKDIR /app

COPY demo/target-service/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY demo/target-service/server.py ./server.py
COPY demo/target-service/hello.proto ./hello.proto

EXPOSE 8080 50051

CMD ["python", "server.py"]
