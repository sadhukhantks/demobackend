FROM python:3.12-slim

WORKDIR /app

# uv.lock pins every dep (incl. the dlib override -> prebuilt dlib-bin, no compile)
COPY --from=ghcr.io/astral-sh/uv:0.8.4 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
ENV PATH="/app/.venv/bin:$PATH"

COPY . .

EXPOSE 9000
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "9000", "--timeout-keep-alive", "650"]
