FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --uid 10001 --create-home orca && mkdir /data && chown orca:orca /data
COPY orca_app ./orca_app
USER orca
ENV ORCA_HOST=0.0.0.0 ORCA_PORT=8000 ORCA_DATABASE=/data/orca.sqlite3 ORCA_CONTAINER=true
EXPOSE 8000
CMD ["python", "-m", "orca_app"]
