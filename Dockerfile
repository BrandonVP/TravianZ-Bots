# Image holds only Python + dependencies; the bot's code and state are mounted from the
# dataset at /app, so code edits only need a container restart, not a rebuild.
FROM python:3.12-slim
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
WORKDIR /app
ENV PYTHONUNBUFFERED=1 TRAVBOT_STATE_DIR=/app/state
CMD ["python", "-m", "travbot.main"]
