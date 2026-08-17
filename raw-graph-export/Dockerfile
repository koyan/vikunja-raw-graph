FROM python:3.13-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY export_raw_graph.py server.py ./
ENTRYPOINT ["python"]
CMD ["export_raw_graph.py", "--out", "/data/raw_graph.svg"]
