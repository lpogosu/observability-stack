FROM python:3.11-slim

# Dependencies are installed before the source is copied so that editing a
# handler does not invalidate the pip layer.
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Running as root inside a container that accepts arbitrary HTTP is a habit worth
# not having, even in a demo stack.
RUN useradd --no-create-home --uid 10001 appuser
USER 10001

EXPOSE 8000
# Exactly one worker, and that is a correctness requirement rather than a
# capacity choice. Each uvicorn worker is a separate process with its own metric
# registry and its own fault state: with two of them a scrape would report one
# worker's view of the traffic, and an injected fault would apply to roughly half
# the requests. The handlers are async, so a single worker serves the injected
# 700 ms delays concurrently anyway - they are awaits, not blocked threads.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
