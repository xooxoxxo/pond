"""Mailbox daemon (the Pond) — local queue, Telegram transport."""
__version__ = "0.1.0"

# The Pond's release, reported by GET /version: semver (MAJOR.MINOR.PATCH), see
# CHANGELOG.md. Hook releases name the Pond range they need (`^1.1.0`), so a minor
# or patch bump never forces a Hook update. A major is only for a breaking change:
# a removed route, a changed meaning, or required auth that isn't behind a switch.
VERSION = "1.16.0"
ADMISSION_FEATURE = "admission"

# The features this Pond has, listed by GET /version (1.8.0). Clients gate a new
# feature on its name here, never on VERSION, so the order Pond branches merge in
# doesn't change client code. A Pond without the `features` key has none of the
# features that are only ever read from this list.
FEATURES = (
    "answers", "blocking", "optional", "fileLinks", "corrections", "bugs",
    "messages", "requirements", "longText", "sharedProgress", "askWarning",
    "appendSteps",
    # Pond 1.11.0: POST /owner/revoke-request, and created/lastUsed/maxKeys on GET /owner.
    "ownerRevokeRequest", "ownerKeyDates",
    # Pond 1.13.0: `fields`, a settings table on a step or sub-step.
    "fields",
    # Pond 1.14.0: `brief`, the background of an ask. "askAbout" (POST /asks/{id}/ask-about)
    # is not here: Service.features() adds it only while the Pond has an Anthropic API key.
    "brief",
    # Pond 1.15.0: an owner's answer text (free text, or the words on a pick) takes 8000
    # characters, as a message does (1000 before).
    "longAnswers",
)

# Kept for 0.9.x Hooks and clients, which read it; new code reads VERSION.
# 1: asks, events, resolve, reply, reopen. 2: step questions and answers.
# 3: blocking and warning, GET /version. 4: optional, file on a step, path on a link
# (Pond 1.0.0). 5: step ids, revisions, amend, accept, problems, the owner key.
# Pond 1.2.0 (Hook bugs, messages) is additive and keeps 5: nothing a 5 client reads changed.
# Pond 1.3.0 (requirements, progress) is additive and keeps 5.
# Pond 1.4.0 (choice objects, 8000-character messages and reports) is additive and keeps 5:
# a client sends X-Hook-Protocol: 6 to read choice objects, and an older one gets labels.
# Pond 1.10.0 (documents: `doc`, `docs`, reading and releasing them) is additive and keeps 5.
# Pond 1.12.0 (a total limit on stored documents, 409 document store full) is additive and keeps 5.
# Pond 1.13.0 (`fields` on a step) is additive and keeps 5.
# Pond 1.14.0 (`brief`, POST /asks/{id}/ask-about) is additive and keeps 5.
# Pond 1.15.0 (8000-character answers, `longAnswers`) is additive and keeps 5.
PROTOCOL = 5
