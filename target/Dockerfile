FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY stub_server.py .

EXPOSE 8080

CMD ["python", "stub_server.py"]
