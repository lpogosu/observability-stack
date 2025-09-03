FROM python:3.11-slim

WORKDIR /srv
COPY loadgen ./loadgen

RUN useradd --no-create-home --uid 10002 loadgen
USER 10002

ENTRYPOINT ["python", "-m", "loadgen"]
CMD ["--scenario", "normal"]
