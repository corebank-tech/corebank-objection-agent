FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY objection_agent ./objection_agent
USER nobody
CMD ["python", "-m", "objection_agent"]
