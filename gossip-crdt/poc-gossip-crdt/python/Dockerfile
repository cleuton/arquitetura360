FROM python:3.11-slim

WORKDIR /app
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY servidor.py /app/servidor.py
RUN pip install --no-cache-dir aiohttp

ENV PORTA=5000 COMPANHEIROS=""
CMD ["python", "/app/servidor.py"]
