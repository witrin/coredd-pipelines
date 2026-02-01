FROM python:3.13-slim AS pipelines

RUN adduser --disabled-password --gecos "" dagster \
    && mkdir -p /srv/bug-request/src /etc/dagster /var/lib/dagster /var/log/dagster \
    && chown -R dagster:dagster /srv/bug-request /etc/dagster /var/lib/dagster /var/log/dagster

WORKDIR /usr/local/src/

COPY pyproject.toml requirements.lock ./

RUN pip install uv \
    && uv pip sync --system requirements.lock \
    && pip install debugpy

ENV DAGSTER_HOME=/etc/dagster \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/usr/local/lib/python3.13/site-packages:/usr/local/src \
    LOG_FORMAT=json \
    LOG_LEVEL=INFO

RUN printf "/usr/local/src/components\nimport logs\n" > \
    /usr/local/lib/python3.13/site-packages/99-logging.pth \
    && printf "/usr/local/src/components\nimport exceptions\n" > \
    /usr/local/lib/python3.13/site-packages/99-exceptions.pth

COPY .dagster/cloud/dagster.yaml .dagster/cloud/workspace.yaml /etc/dagster/

COPY .dagster/components/ ./components/
COPY src/ ./benchmark/

USER dagster

EXPOSE 8080

ENTRYPOINT []

FROM pipelines AS job

COPY .dagster/cloud/job/dagster.yaml /etc/dagster/

ENTRYPOINT ["/usr/local/bin/python", "/usr/local/src/components/entrypoint.py"]

FROM google/cloud-sdk:slim AS console

RUN apt update -y && \
    apt install -y wget gpg lsb-release && \
    wget -O- https://apt.releases.hashicorp.com/gpg | gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] \
    https://apt.releases.hashicorp.com $(lsb_release -cs) main" > /etc/apt/sources.list.d/hashicorp.list && \
    apt update -y && \
    apt install -y terraform && \
    terraform -version && \
    apt clean && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY .cloud/ /workspace/

ENTRYPOINT ["/bin/bash"]

FROM argilla/argilla-server:v2.8.0 AS evaluation

COPY .argilla/oauth.yml /etc/argilla/oauth.yml
