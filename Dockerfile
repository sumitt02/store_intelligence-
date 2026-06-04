FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY data/ /data/
COPY dashboard/ ./dashboard/

ENV DB_PATH=/data/store_intelligence.db
ENV POS_PATH=/data/pos_transactions.csv
ENV LOG_LEVEL=INFO
ENV PORT=8000

EXPOSE 8000

COPY pipeline/ ./pipeline/
COPY start.sh .
RUN chmod +x start.sh

CMD ["./start.sh"]
