# Cloud Run API server — FastAPI/uvicorn on port 8080.
# No system packages needed: Pedra and content_plan only use Python requests/httpx.

FROM python:3.11-slim-bullseye

WORKDIR /MoneyPrinterTurbo
RUN chmod 777 /MoneyPrinterTurbo

ENV PYTHONPATH="/MoneyPrinterTurbo"

# Install Python dependencies.
COPY requirements.txt ./
RUN pip install --no-cache-dir --retries 3 --timeout 60 -r requirements.txt

# Copy source.
COPY . .

# Create default config from example.
RUN cp config.example.toml config.toml

EXPOSE 8080

CMD ["python", "main.py"]
