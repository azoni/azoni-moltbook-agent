FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port for API
EXPOSE 8000

# Default command: run the API server
# To run heartbeat instead: docker run ... python -m heartbeat.scheduler
CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8000"]
