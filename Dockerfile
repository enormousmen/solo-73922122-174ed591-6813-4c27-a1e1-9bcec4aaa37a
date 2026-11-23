FROM python:3.12-slim

# util-linux provides mount/umount for the in-container disk-swap demo
# (only needed by demo/entrypoint.sh; the API itself never invokes them).
RUN apt-get update \
    && apt-get install -y --no-install-recommends util-linux curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
