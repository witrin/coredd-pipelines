FROM python:3.11-slim

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
