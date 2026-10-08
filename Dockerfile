FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY murmuration ./murmuration
RUN pip install --no-cache-dir ".[aws]"
USER nobody
ENTRYPOINT []
CMD ["murmuration", "run"]
