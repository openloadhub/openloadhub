FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=5

WORKDIR /app

RUN set -eux; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update; \
    apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install -y --no-install-recommends \
        curl \
        dnsutils \
        iproute2 \
        iputils-ping \
        netcat-traditional \
        procps; \
    rm -rf /var/lib/apt/lists/*

COPY backend/requirements-common.txt ./requirements-common.txt
COPY backend/ptp-admin/requirements.txt ./requirements.txt
RUN sed -i 's#../requirements-common.txt#./requirements-common.txt#g' requirements.txt
RUN pip install --no-cache-dir -r requirements-common.txt -r requirements.txt

COPY backend/common ./common
COPY docs/public/examples ./docs/public/examples
COPY backend/ptp-admin/ .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
