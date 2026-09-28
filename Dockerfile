FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ARG HUNTER_SOURCE_SHA=UNSPECIFIED
ENV HUNTER_SOURCE_SHA=$HUNTER_SOURCE_SHA
WORKDIR /app
COPY hunter-global/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY hunter-global/ /app/
ENTRYPOINT ["python", "/app/runner.py"]
