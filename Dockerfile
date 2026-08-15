# One image, two roles. The engine and the mock upstream services share a
# dependency set, and compose picks the entrypoint — cheaper to build and
# guarantees both run the same interpreter and library versions.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

# Dependencies first so edits to source do not invalidate the install layer.
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

COPY app ./app
COPY config ./config
COPY mock_services ./mock_services

# Overridden by compose for the mock services container.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
