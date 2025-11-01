FROM python:3.11-slim AS pipelines

ENV DAGSTER_HOME=/etc/dagster \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/usr/local/lib/python3.11/site-packages:/usr/local/src

RUN adduser --disabled-password --gecos "" dagster \
   && mkdir -p /srv/bug-request/src /etc/dagster /var/lib/dagster /var/log/dagster \
   && chown -R dagster:dagster /srv/bug-request /etc/dagster /var/lib/dagster /var/log/dagster

WORKDIR /usr/local/src/

COPY pyproject.toml requirements.lock ./
COPY src/ ./benchmark/
COPY .dagster/cloud/dagster.yaml /etc/dagster/dagster.yaml

RUN pip install --no-cache-dir -r requirements.lock

USER dagster

EXPOSE 8080

CMD ["dagster", "api", "grpc", "-h", "0.0.0.0", "-p", "8080", "-f", "/usr/local/src/benchmark/definitions.py"]

FROM google/cloud-sdk:slim AS console

RUN apt-get update -y && \
    apt-get install -y wget gpg lsb-release && \
    wget -O- https://apt.releases.hashicorp.com/gpg | gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] \
    https://apt.releases.hashicorp.com $(lsb_release -cs) main" > /etc/apt/sources.list.d/hashicorp.list && \
    apt-get update -y && \
    apt-get install -y terraform && \
    terraform -version && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY .cloud/ /workspace/

ENTRYPOINT ["/bin/bash"]
