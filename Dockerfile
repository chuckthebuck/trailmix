FROM python:3.12-slim
WORKDIR /app
COPY server.py turso_db.py trailmix.html ./
RUN useradd -r -u 10001 app && mkdir /data && chown app /data
USER app
ENV TRAILMIX_DB=/data/trailmix.sqlite3
VOLUME ["/data"]
EXPOSE 8080
CMD ["python", "-u", "server.py"]
