FROM python:3.12-slim
WORKDIR /app

# Copy uv from the official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Compile dependencies
ENV UV_COMPILE_BYTECODE=1
COPY pyproject.toml ./
RUN uv pip install --system --no-cache -r pyproject.toml

# Copy the rest
COPY src/ /app/src/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

CMD ["uvicorn", "src.mcp_server:app", "--host", "0.0.0.0", "--port", "8000"]
