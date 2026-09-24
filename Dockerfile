FROM python:3.13-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
COPY gunicorn_conf.py .

EXPOSE 8110
CMD ["gunicorn", "-c", "gunicorn_conf.py", "app.main:app"]
