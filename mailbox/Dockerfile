FROM python:3.12-alpine

# tzdata: python:alpine ships no zoneinfo, and musl resolves a missing zone as UTC in silence.
RUN apk add --no-cache tzdata && adduser -D -u 1000 mailbox

WORKDIR /app

# Runtime files only, named one by one: no tests, docs, env files or host scripts reach the image.
COPY __init__.py __main__.py admin.py answers.py ask_about.py capture.py clock.py ct_client.py \
     docs.py household.py instructable.py link_icon.py link_page.py migrations.py notification.py \
     owner.py requirements.py revisions.py server.py service.py store.py telegram.py \
     /app/mailbox/

RUN mkdir -p /data /backups && chown mailbox:mailbox /data /backups

USER mailbox

ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=3s --start-period=90s \
    CMD wget -qO- "http://127.0.0.1:${MAILBOX_PORT:-8790}/health" || exit 1

EXPOSE 8790

CMD ["python3", "-m", "mailbox"]
