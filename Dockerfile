FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=Europe/Madrid
WORKDIR /app
COPY aimharder_bot_render.py .
CMD ["python", "aimharder_bot_render.py"]
