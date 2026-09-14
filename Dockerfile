FROM python:3.12-slim
WORKDIR /app
COPY deploy/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY waystone ./waystone
RUN pip install --no-cache-dir --no-deps .
ENV PYTHONUNBUFFERED=1 WAYSTONE_DB=/data/waystone.sqlite
CMD ["uvicorn","waystone.api:create_app","--factory","--host","0.0.0.0","--port","8900","--no-access-log"]
