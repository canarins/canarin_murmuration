FROM python:3.12-slim
WORKDIR /app
# RDS CA bundle for TLS to MySQL (same path/convention as Plumage)
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends curl ca-certificates \
    && curl -fsSL https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
         -o /etc/ssl/certs/rds-ca-bundle.pem \
    && apt-get purge -y -qq curl && apt-get autoremove -y -qq && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY murmuration ./murmuration
COPY db ./db
RUN pip install --no-cache-dir . boto3
USER nobody
ENTRYPOINT []
CMD ["murmuration", "run"]
