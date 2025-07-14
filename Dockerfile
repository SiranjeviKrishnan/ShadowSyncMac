FROM python:3.11-slim

# Create app directory
WORKDIR /app

# Copy files
COPY requirments.txt .
RUN pip install --no-cache-dir -r requirments.txt

COPY sync_engine.py .

# Optional: create a volume mount point
VOLUME ["/data"]

# Default command
CMD ["python", "app.py"]
