FROM python:3.12-slim
WORKDIR /app
COPY apps/api/pyproject.toml apps/api/
COPY apps/api/dispatch apps/api/dispatch
COPY data data
RUN pip install --no-cache-dir -e apps/api
ENV PORT=8787
EXPOSE 8787
CMD ["sh", "-c", "python -m uvicorn dispatch.main:app --host 0.0.0.0 --port ${PORT} --app-dir apps/api"]
