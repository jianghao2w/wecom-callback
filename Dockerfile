FROM mcr.microsoft.com/playwright/python:v1.64.1-noble
RUN apt-get update && apt-get install -y --no-install-recommends python3-venv && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt /app/
RUN python -m venv /app/.venv && /app/.venv/bin/pip install --no-cache-dir -r requirements.txt
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY *.py /app/
CMD ["python", "app.py"]
