# base image
FROM python:3.11-slim-buster

# workdir
WORKDIR /app

# deps first, so this layer caches until requirements.txt changes
COPY requirements.txt /app/
RUN pip install -r requirements.txt

# app code
COPY . /app

# port
EXPOSE 8501

# command
CMD ["streamlit", "run", "app.py", "--server.port=8501"]
