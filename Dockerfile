FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY food ./food
COPY recognition ./recognition
COPY scripts ./scripts
COPY seed ./seed
COPY static ./static

ENV GYMTRACK_DB=/data/gymtrack.db \
    GYMTRACK_FOODS_DB=/data/foods.db \
    PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
