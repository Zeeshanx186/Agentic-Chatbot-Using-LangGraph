FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libgomp1 libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
COPY deploy/requirements.txt requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt
RUN pip install onnxruntime==1.29.0
COPY dexter/ dexter/
COPY chainlit.md .
COPY .chainlit/ .chainlit/
COPY public/ public/
ARG APP_UID=1001
RUN useradd --uid ${APP_UID} --create-home dexter && mkdir -p /home/dexter/.cache && chown -R dexter:dexter /app /home/dexter
USER dexter
EXPOSE 8000
CMD ["chainlit", "run", "dexter/chainlit_app.py", "--host", "0.0.0.0", "--port", "8000", "--headless"]
