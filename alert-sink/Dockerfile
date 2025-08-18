FROM python:3.11-slim

WORKDIR /srv
COPY alert_sink ./alert_sink

RUN useradd --no-create-home --uid 10003 sink
USER 10003

EXPOSE 9095
CMD ["python", "-m", "alert_sink.server"]
