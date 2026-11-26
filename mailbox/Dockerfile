# Pinned by digest: python 3.12.16 on Alpine 3.24, the multi-arch index (linux/amd64 and
# linux/arm64 among others), built 2026-11-05. Bump tag and digest together, to a build at
# least 5 days old: docker buildx imagetools inspect python:<tag>.
FROM python:3.12.16-alpine3.24@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89

# tzdata: the Pond needs zoneinfo, and musl resolves a missing zone as UTC in silence. Pinned
# to the version the pinned base already carries (built 2026-11-04), so apk installs nothing
# new; a base bump that changes it fails here until this pin moves with it.
RUN apk add --no-cache tzdata=2026f-r0 && adduser -D -u 1000 mailbox

WORKDIR /app

# Runtime files only, named one by one: no tests, docs, env files or host scripts reach the image.
COPY __init__.py __main__.py admin.py admission.py migration_006_admission.py answers.py ask_about.py capture.py clock.py ct_client.py \
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
