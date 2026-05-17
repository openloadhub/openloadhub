FROM eclipse-temurin:17-jre AS jdk17
FROM python:3.11-slim AS runtime-base

ARG TARGETARCH
ARG INSTALL_CHROMIUM=0
ARG JMETER_VERSION=5.4.3
ARG JMETER_DOWNLOAD_URL=https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-5.4.3.tgz
ARG JMETER_TGZ_SHA512=e88802cc0dfcd6a2c8554911ae4574d7cfafcc8c6be6ade810b4677b7351831b0680d81cf2b0fb5bb4b9b3cf437528a044d7da74214a1bee351b273dbb53e439
ARG JMETER_GRPC_PLUGIN_URL=https://github.com/zalopay-oss/jmeter-grpc-request/releases/download/v1.2.6/jmeter-grpc-request.jar
ARG JMETER_GRPC_PLUGIN_SHA256=3bdd71cfb13634d29379e9d73f21912330f0416e02386e853f513ece679aafae
ARG K6_VERSION=0.54.0
ARG K6_AMD64_SHA256=c7f03434854f837b6790ee81572e4b0f955241974c79a43cbb9f8d0fef069589
ARG K6_ARM64_SHA256=6be08e8578af0ca79ce7d5f8f6e1adb4cae080d6752a342295260fe246655b1f
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JAVA_HOME=/opt/java/openjdk \
    JMETER_HOME=/opt/apache-jmeter-5.4.3 \
    K6_BINARY=/usr/local/bin/k6 \
    K6_BIN=/usr/local/bin/k6 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=5 \
    PTP_K6_SCENARIO_CONFIG_PATCH_ENABLED=0 \
    K6_BROWSER_ARGS=no-sandbox \
    K6_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium \
    PATH=/opt/java/openjdk/bin:${PATH}

WORKDIR /app

RUN set -eux; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update; \
    apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install -y --no-install-recommends \
        ca-certificates \
        curl \
        dnsutils \
        iproute2 \
        iputils-ping \
        netcat-traditional \
        procps; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    if [ "${INSTALL_CHROMIUM}" = "0" ]; then \
        echo "Skipping chromium install"; \
        exit 0; \
    fi; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update; \
    apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install -y --no-install-recommends chromium; \
    rm -rf /var/lib/apt/lists/*

COPY --from=jdk17 /opt/java/openjdk /opt/java/openjdk

RUN set -eux; \
    mkdir -p /opt; \
    curl --connect-timeout 15 --max-time 180 --retry 3 --retry-delay 2 -fsSL "${JMETER_DOWNLOAD_URL}" -o /tmp/apache-jmeter.tgz; \
    printf '%s  %s\n' "${JMETER_TGZ_SHA512}" /tmp/apache-jmeter.tgz | sha512sum -c -; \
    tar -xzf /tmp/apache-jmeter.tgz -C /opt; \
    rm /tmp/apache-jmeter.tgz; \
    test -x "${JMETER_HOME}/bin/jmeter"

RUN set -eux; \
    mkdir -p "${JMETER_HOME}/lib/ext"; \
    curl --connect-timeout 15 --max-time 180 --retry 3 --retry-delay 2 -fsSL "${JMETER_GRPC_PLUGIN_URL}" -o "${JMETER_HOME}/lib/ext/jmeter-grpc-request-v2.jar"; \
    printf '%s  %s\n' "${JMETER_GRPC_PLUGIN_SHA256}" "${JMETER_HOME}/lib/ext/jmeter-grpc-request-v2.jar" | sha256sum -c -; \
    python -c 'exec("""from pathlib import Path\nimport shutil\nimport zipfile\njar_path=Path(\"/opt/apache-jmeter-5.4.3/lib/ext/jmeter-grpc-request-v2.jar\")\ntmp_path=jar_path.with_suffix(\".stripped.jar\")\nstrip_prefixes=(\n    \"org/slf4j/\",\n    \"org/apache/logging/slf4j/\",\n    \"META-INF/maven/org.apache.logging.log4j/log4j-slf4j-impl/\",\n)\nwith zipfile.ZipFile(jar_path,\"r\") as src, zipfile.ZipFile(tmp_path,\"w\") as dst:\n    for info in src.infolist():\n        if any(info.filename.startswith(prefix) for prefix in strip_prefixes):\n            continue\n        dst.writestr(info, src.read(info.filename))\nshutil.move(tmp_path, jar_path)\n""")'

RUN set -eux; \
    arch="${TARGETARCH:-amd64}"; \
    case "${arch}" in \
        amd64) k6_arch="amd64"; k6_sha="${K6_AMD64_SHA256}" ;; \
        arm64) k6_arch="arm64"; k6_sha="${K6_ARM64_SHA256}" ;; \
        *) echo "Unsupported TARGETARCH=${arch}" >&2; exit 1 ;; \
    esac; \
    k6_archive="k6-v${K6_VERSION}-linux-${k6_arch}.tar.gz"; \
    curl --connect-timeout 15 --max-time 180 --retry 3 --retry-delay 2 -fsSL "https://github.com/grafana/k6/releases/download/v${K6_VERSION}/${k6_archive}" -o "/tmp/${k6_archive}"; \
    printf '%s  %s\n' "${k6_sha}" "/tmp/${k6_archive}" | sha256sum -c -; \
    tar -xzf "/tmp/${k6_archive}" -C /tmp; \
    install -m 0755 "/tmp/k6-v${K6_VERSION}-linux-${k6_arch}/k6" "${K6_BINARY}"; \
    rm -rf "/tmp/${k6_archive}" "/tmp/k6-v${K6_VERSION}-linux-${k6_arch}"; \
    "${K6_BINARY}" version

COPY backend/requirements-common.txt ./requirements-common.txt
COPY backend/ptp-agent/requirements.txt ./requirements.txt
RUN sed -i 's#../requirements-common.txt#./requirements-common.txt#g' requirements.txt
RUN pip install --no-cache-dir -r requirements-common.txt -r requirements.txt

FROM runtime-base AS app

COPY backend/common ./common
COPY backend/ptp-agent/ .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9096"]
