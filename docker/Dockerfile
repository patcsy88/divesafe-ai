FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 divesafe
USER divesafe

EXPOSE 8000
CMD ["uvicorn", "divesafe.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
