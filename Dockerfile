FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin libgdal-dev libpq-dev gcc g++ \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev,sim,timeseries]" 2>/dev/null || pip install --no-cache-dir ".[sim,timeseries]"

COPY . .

EXPOSE 8000
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
