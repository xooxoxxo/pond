# Pond

The Pond is the small server behind [Hook](https://github.com/xooxoxxo/hook):
a queue of asks from coding agents to their owner, a Telegram bot that carries
them to the owner's phone, and the HTTP API that Hook and agent clients use to
post, read and answer them. It is one Python process with SQLite storage and no
third-party Python dependencies, shipped as a single container.

One Pond serves one owner and one context (for example `personal`): one
Telegram bot, one queue, one API token. The Python package is `mailbox/`
(`python3 -m mailbox`), and environment variables keep their `MAILBOX_` prefix.

Licensed under the MIT License (see `LICENSE`). Security reports: see
`SECURITY.md`.

## Versions

The Pond is versioned with semver (`mailbox/__init__.py` `VERSION`, notes in
`CHANGELOG.md`). Hook releases name the Pond range they need, npm style
(`^1.1.0`), so a minor or patch Pond release never forces a Hook update; only
a major one (a removed route, a changed meaning, required auth that isn't
behind a switch) does. `/version` still reports `protocol` (5) for Hook 0.9.x
and its clients, which compare that integer.

## Run it

You need Docker with Compose, a Telegram bot token from
[@BotFather](https://t.me/BotFather), and the owner's numeric Telegram user id.

1. Copy the `mailbox/` folder of this repository to the host, for example
   `git clone https://github.com/xooxoxxo/pond.git && cd pond/mailbox`.
2. Write `.env` next to `compose.yaml` from `.env.example` (mode 600) with at
   least `TELEGRAM_BOT_TOKEN`, `MAILBOX_OWNER_ID`, `MAILBOX_CONTEXT` and `TZ`.
   Never commit it.
3. `sudo ./prepare-host.sh` sets `backups/` to 700, owned by you, and refuses
   to continue without a 600 `.env` carrying those four keys. Safe to re-run.
4. `docker compose up --build -d`. `/health` returns 503 until the first
   successful Telegram poll, then 200. The image creates `/data` and
   `/backups` owned by uid 1000 so a fresh named volume is writable by the
   daemon. Check the container clock: `docker compose exec -T mailbox date`
   prints the local time of `TZ`, and the startup line reads, for example with
   `TZ=Etc/UTC`, `capture: …, notify: … (tz Etc/UTC +0000 UTC)`: the zone the
   daemon resolved with its offset, not the `TZ` value. The image installs
   `tzdata` for that; `python:alpine` has none and would run any other zone
   as UTC in silence.
5. The Pond token (used by every API call except `/health` and `/i/<uuid>`)
   is generated once on first start and written to `backups/mailbox-token`
   (mode 600). It is never logged. Give it to your clients without displaying
   it, for example by copying the file.
6. Check it answers:
   `curl -s -H "Authorization: Bearer $(cat backups/mailbox-token)" http://127.0.0.1:8790/version`.

The server listens on port 8790 (`MAILBOX_PORT`). It speaks plain HTTP: put
it behind a TLS-terminating reverse proxy or a private network before any
client reaches it over the internet. Only `GET /health` and `GET /i/<uuid>`
work without the token.

### Configuration

| Variable | Required | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | The bot's token from @BotFather. |
| `MAILBOX_OWNER_ID` | yes | The owner's numeric Telegram user id. Updates from anyone else are dropped and counted. |
| `MAILBOX_CONTEXT` | yes | The context name this Pond serves, for example `personal`. A post for another context is `403`. |
| `TZ` | yes | IANA zone for the nightly backup and for capture dates. Compose refuses to start without it. |
| `MAILBOX_PORT` | no | Listening port inside the container, default 8790. |
| `MAILBOX_HOST_PORT` | no | Host port `compose.yaml` publishes, default 8790. |
| `MAILBOX_IMAGE` | no | Image tag `compose.yaml` builds and runs, default `pond:local`. |
| `MAILBOX_DATA_DIR`, `MAILBOX_BACKUP_DIR` | no | Default `/data` and `/backups` (set by `compose.yaml`). |
| `MAILBOX_RELAY_OPEN_BASE` | no | Public link base such as `https://pond.example/i`: adds "📋 Open in Hook" to each ask and `openUrl` to `POST /asks` answers. |
| `MAILBOX_TELEGRAM_MODE` | no | `full` (default) or `alert`; see below. |
| `MAILBOX_CT_URL`, `MAILBOX_CT_TOKEN` | no | Capture into a Central Time install (both or neither). |
| `MAILBOX_NOTIFY_TOKEN` | no | A token that opens `POST /notify` only. |
| `MAILBOX_HOST_GATEWAY` | no | The host's LAN address, when `host.docker.internal` is filtered. |
| `MAILBOX_BRIEF_ASKS` | no | `0` drops the "Open asks: N" line from the morning brief. |
| `MAILBOX_MEMBERS` | no | Household mode; see the end of this document. |
| `ANTHROPIC_API_KEY` or `ANTHROPIC_API_KEY_FILE` | no | Turns on Ask about this. |

### Upgrades and rollback

The 1.1.0 and 1.2.0 migrations are additive: they add side tables (1.2.0 adds
`bugs`, schema 2) and never a column to `items` or `events`, and older code
ignores the new tables. Roll back by redeploying the previous
image; asks posted during a rollback get their
revision 1 on the next start. Bugs filed before a rollback read as plain asks
to 1.1.0 code.

1.10.0 (schema 4) adds the side tables `docs` and `ask_docs` the same way. On a
Pond rolled back to 1.9.0 after documents were used, an ask whose body holds `doc`
refuses added steps (`…doc: unknown key`) and can only be corrected by dropping
`doc`; every document read answers `404`; the `docs_enabled` setting survives, so
the switch is on again after the next update.

**Admin command** (inside the container, no token):
`docker compose exec -T mailbox python3 -m mailbox.admin corrections on|off|status`
opens or closes `POST /asks/{id}/amend`. It is off until both devices run a Hook
that reviews corrections.
`docker compose exec -T mailbox python3 -m mailbox.admin appends on|off|status`
opens or closes `POST /asks/{id}/append` (1.9.0). It is on by default.
`docker compose exec -T mailbox python3 -m mailbox.admin docs on|off|status`
opens or closes documents on asks (1.10.0): the `docs` request member and `doc`
in an instructable. It is off by default, and `GET /version` lists `docs` only while
it is on. While it is off, an ask posted with documents can still be corrected and
added to as long as it keeps only the documents it holds. Turning it off keeps every
document already stored.

## API

All endpoints except public `GET /health` and `GET /i/<uuid>` require
`Authorization: Bearer <mailbox-token>` and answer `401` without it.
`GET /version` also requires the token.

| Method | Path | Purpose | Status codes |
|---|---|---|---|
| `GET` | `/health` | liveness probe, no auth | `200` healthy, `503` unhealthy |
| `GET` | `/i/<uuid>` | public fixed Hook launch page; validates only the UUID, never reads an ask | `200` HTML, `404` |
| `GET` | `/version` | `{"service": "pond", "version": "1.15.0", "protocol": 5, "features": ["answers", …, "appendSteps", "ownerRevokeRequest", "ownerKeyDates", "fields", "brief", "longAnswers", "docs", "askAbout"]}`; Hook checks `version` against the range it needs, 0.9.x compares `protocol`; a client gates a new feature on its name in `features` (1.8.0), never on the version | `200`, `401` |
| `POST` | `/asks` | post an ask (deduped by payload key within 24h); optional `instructable` (Hook instructable v1, see `instructable.py`), `ask` then defaults to its title; its steps get ids (`s1`, `s3.1`) unless every step has one (an id may not read as a step number such as `2` or `3.1`), and it becomes revision 1. Optional `editKeyHash` (64 lowercase hex, the SHA-256 of the poster's edit key). The response carries `revision` `{rev, hash}` and `editable` (true only for the stored hash, also on a dedupe hit). Optional `docs` (1.10.0, see Documents below) | `201` created, `200` deduped, `400` missing fields, `invalid editKeyHash` or `{"error": "invalid instructable", "details": [...]}`, `403` wrong context, `409` documents not enabled, `400` `{"error": "invalid documents", "details": [...]}`, `409` document store full (1.12.0), `502` Telegram send failed |
| `POST` | `/asks` with `kind: "bug"` | a Hook bug (1.2.0): `{project, context, kind: "bug", signature, instructable}`, owner key always, never sent to Telegram. Deduped by `signature` (12 lowercase hex) while a bug with it is open: the repeat adds an occurrence. The item carries `kind: "bug"` and `bug` `{signature, count, firstSeen, lastSeen}`. A bug takes no documents (1.10.0): a `doc` or a `docs` member is refused as for an ask, `409` documents not enabled or `400` invalid documents | `201` `{"item", "occurrences": 1}`, `200` `{"item", "deduped": true, "occurrences": n}`, `400` invalid kind, invalid signature, missing fields or invalid instructable, `403` owner key required, unknown owner key or wrong context |
| `GET` | `/asks?status=open&days=14&kind=bug` | list asks; `kind` (1.2.0) is `bug` or `ask`, absent for both | `200`, `400` invalid query |
| `GET` | `/asks/{id}` | fetch one ask with its events; with `X-Hook-Protocol: 5` the item carries `revision` `{rev, hash, prev, at, reason, body, acceptedRev}` and the response `revisions` (every one); an append's revision adds `"kind": "append"`, and an ask waiting for its session's next steps adds `stepWait` (1.9.0). Without the header a corrected ask is a stub telling the owner to update Hook | `200`, `404` not found |
| `GET` | `/asks/{id}/docs/{sha256}` | read a document the ask keeps (1.10.0): the exact stored bytes, with `Content-Type: text/plain; charset=utf-8`, `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`. The token as for `GET /asks/{id}`; works with the `docs` switch off. Transcript: `tests/doc-wire-vectors.json` | `200` text, `401`, `404` `{"error": "not found"}` (unknown ask, a hash that is not 64 lowercase hex, or one the ask never linked), `410` `{"error": "document no longer kept"}` once the ask released it, also when another ask still keeps the same text |
| `POST` | `/asks/{id}/docs/release` | release a closed ask's documents (1.10.0): the token only, no owner or edit key, so an agent that took the work over can release them; the body is ignored. Deletes each text the ask linked that no other ask still keeps; writes no event and changes nothing on the ask. The Pond also releases them itself 7 days after the ask closed. A released or expired document comes back only with a new revision that resends its text (a correction that changes nothing is refused as `no change` before documents are looked at) | `200` `{"released": n}` (the documents this call released; `0` on a second call or for an ask without documents), `401`, `404` not found, `400` invalid json, `409` `{"error": "ask is open"}` (nothing deleted) |
| `POST` | `/asks/{id}/amend` | correct an open ask: `{editKey, baseRev, baseHash, reason, instructable}` → revision N+1 by compare-and-swap; at most 10 corrections (appends are counted apart), 60 s apart; one that only adds steps is stored as an append, and refused as the append route refuses it (`409` appends not enabled or append limit reached, `400` invalid steps over 10 steps) when it fails the append gates. Telegram strikes the old message through and sends a Corrected reply with Skip only | `200` `{"item", "revision": {rev, hash, changed, added, removed, moved, top}}`, `404`, `409` corrections not enabled, `400` invalid json or missing fields, `403` not editable or wrong edit key, `409` ask is closed, stale revision (with `rev`, `hash`), revision limit reached, appends not enabled or append limit reached, `429` too soon (`retryAfter`), `400` invalid instructable, invalid amendment, invalid steps or no change, `409` documents not enabled, `400` invalid documents, `409` document store full (1.12.0) |
| `POST` | `/asks/{id}/append` | add steps after the last step of an open ask (1.9.0): `{editKey, baseRev, baseHash, note, steps}` → revision N+1 with kind `append`; 1 to 10 steps, `note` 1 to 200 characters on one line; at most 10 appends (counted apart from corrections), 15 s after any revision; steps without ids get `r<rev>-<n>`; the result passes the post rules. When the owner had accepted the current revision, the append is accepted with it, so acts on unchanged steps keep counting, and the owner's Done bound to an older rev is refused with `steps_added`. Telegram adds "Steps added: see below" to the old message (its question buttons stay, Done and Skip go) and sends the new steps as a reply with Done bound to the new rev, silently when the owner acted on the ask in the last 2 minutes. Transcript: `tests/append-wire-vectors.json`. Details name the request's steps (`steps[0].code: secret-like value`) | `200` `{"item", "revision": {rev, hash, kind, added: [{id, path}]}, "openUrl"?}`, `404`, `409` appends not enabled, `400` invalid json or missing fields, `403` not editable or wrong edit key, `409` ask is closed, stale revision (with `rev`, `hash`) or append limit reached, `429` too soon (`retryAfter`), `400` invalid steps (`details`), `409` documents not enabled, `400` invalid documents, `409` document store full (1.12.0) |
| `POST` | `/asks/{id}/accept` | the owner accepted `{rev, hash}` (owner key always): that revision is now in force for Telegram, answers and Done | `200`, `403`, `404`, `409` stale revision |
| `POST` | `/asks/{id}/problems` | the owner reports a problem with a step: `{step, rev, hash, note}` (owner key always; 1-8,000 characters, multi-line so a pasted traceback keeps its lines (1.4.0; one line of 300 before), 20 per ask) | `200`, `403`, `404`, `409` ask is closed, stale revision or too many reports, `400` invalid problem |
| `GET` | `/owner` | `{required, keys: [{id, label, status, created, approvedAt, lastUsed}], self, maxKeys}`: approved and pending keys (never a hash), and `self`, the key matching the `X-Hook-Owner-Key` sent. The Pond token alone reads it, so a device without a working key can list the others. `lastUsed` (1.11.0) is when a request last authenticated with the key, stamped at most once an hour, null when never; `maxKeys` (1.11.0) is the limit, 10 | `200`, `401` |
| `POST` | `/owner/enroll` | `{keyHash, label}`: ask the owner on Telegram to approve this device's key (a 6-digit code to compare; 600 s; a second request while one is pending refuses both; at most 10 keys, 4 before 1.11.0) | `201` `{id, code, expires, status}`, `400`, `409` too many keys or already enrolled |
| `GET` | `/owner/enroll/{id}` | `{id, status}`: pending, approved, rejected or expired | `200`, `404` |
| `POST` | `/owner/require` | `{required}` (owner key always): on at once; off only after the owner taps Turn off on Telegram | `200` `{required}`, `202` `{pending: true}`, `403` |
| `POST` | `/owner/revoke` | `{id}` (owner key always) | `200` `{id, status: "revoked"}`, `403`, `404` |
| `POST` | `/owner/revoke-request` | `{id}` (1.11.0), the Pond token only: ask the owner on Telegram to revoke an approved key, for a device that has no working key of its own. Telegram shows "Revoke Hook on <label> (set up <date>)? Code <code>" with Revoke and Keep; the key is revoked only on the owner's Revoke tap, which also sends the usual "can no longer act as you" line. 600 s; one pending request at a time | `201` `{id, code, expires, status: "pending"}`, `400` missing fields, `401`, `404` unknown key, `409` already revoked, not approved or revoke request pending, `502` telegram send failed |
| `GET` | `/owner/revoke-request/{id}` | `{id, status}` (1.11.0): pending, approved (the key is revoked), rejected (the owner tapped Keep) or expired | `200`, `401`, `404` |
| `POST` | `/asks/{id}/ack` | acknowledge (👀 prefix) | `200`, `404` not found |
| `POST` | `/asks/{id}/steps` | a session note under the ask, sent escaped under "📝 Session note, not part of the ask:" (1500 characters, hidden-character and secret rules) | `200`, `404` not found, `400` missing text or `{"error": "invalid steps", "details": [...]}` |
| `POST` | `/asks/{id}/resolve` | resolve with verdict (`done`/`declined`); optional `actor`: `session` (default) or `owner` (Hook). An owner Done on an ask with `required` steps or a `done` list (1.3.0) waits until nothing is left; Skip and the session's Done never wait. After the owner answers a question with `"then": "steps"` (1.9.0), the owner's Done also waits for the session's next revision, at most 10 minutes; the ask's detail carries `stepWait` `{id, path, until}` meanwhile and Telegram hides Done | `200`, `404` not found, `400` invalid verdict or actor, `409` ask was corrected, `409` `{"error": "steps_added", "rev", "hash"}` (1.9.0), `409` `{"error": "waiting_for_steps", "until", "path"}` (1.9.0), `409` `{"error": "requirements_unmet", "left": [{kind, id?, path?}], "details": ["step 2", "the reply", "a message"]}` |
| `POST` | `/asks/{id}/progress` | the owner's ticks (1.3.0): `{"tick": [ids], "untick": [ids], "rev", "hash"}`, deltas, so two devices merge. Stored as one `progress` event only when it changes the ticked set of an ask with requirements. Owner key as answers | `200` `{"item"}` with `requirements`, `401`, `404`, `400` invalid json or missing fields, `403`, `409` ask is closed or ask was corrected, `400` `{"error": "invalid progress", "details": [...]}` |
| `POST` | `/asks/{id}/reply` | record the owner's reply `{"text": …}` (event `reply`, actor `owner`); Telegram is not touched. With `"kind": "message"` (1.2.0) it is a message to the posting session instead: event `message`, the text verbatim, 1-8,000 characters (1.4.0; 1,000 before; note rules, multi-line), 50 per ask, never an answer | `200`, `404` not found, `400` missing text, invalid kind or `{"error": "invalid message", "details": [...]}`, `409` ask is closed or too many messages |
| `POST` | `/asks/{id}/reopen` | bring a `done` or `dismissed` ask back to `open` (Hook's Reopen); optional `actor`: `owner` (default) or `session`. Clears `completedAt`, records event `reopened`, and edits the Telegram message back to the open ask with Done/Skip (or sends it afresh if the edit fails). Already open: no change, no event | `200`, `404` not found, `400` invalid json or actor |
| `POST` | `/asks/{id}/answers` | the owner's answers to step questions, `{"answers": [{"step": "3", "picked": ["Staging"]}, {"step": "4.1", "text": "…"}]}` (1-20, checked by `answers.py`). A pick of a choice with words (1.4.0) may carry the owner's words beside it: `{"step": "2", "picked": ["Mix"], "text": "…"}`. A free-text answer and the words on a pick are multi-line, 1-8,000 characters (1.15.0, feature `longAnswers`; 1-1,000 before), as a message is (`answers[0].text: longer than 8000 characters`). Telegram never shows an answer's text, only ✓ on the picks. Each becomes one event `answer`, actor `owner`, `text` its compact JSON; the batch is one transaction, the ask stays open, and the Telegram keyboard is redrawn with ✓ on the current picks (best effort) | `200` `{"item"}`, `404` not found, `400` invalid json or missing fields, `409` `{"error": "ask is closed"}` unless open, `400` `{"error": "invalid answers", "details": [...]}` (nothing stored) |
| `POST` | `/asks/{id}/ask-about` | the owner's question about an ask (1.14.0, feature `askAbout`): `{"question"}`, 1-1,000 characters (note rules, multi-line). Claude Haiku 5.5 answers from the ask and its `brief`; the question is stored as event `ask_about_question` (actor `owner`) and the answer as `ask_about_answer` (actor `hook`), together and only on success. The session is not told. Owner key as a message. 20 per ask, 200 per Pond per UTC day. Off (`404`, as an unknown route) without an Anthropic API key | `200` `{"answer", "events": [question, answer]}`, `401`, `403`, `404`, `400` invalid json, missing question or `{"error": "invalid question", "details": [...]}`, `409` ask is closed, `429` too many questions or daily limit reached (with `message`), `502` `{"error": "answer unavailable"}` (nothing stored) |
| `GET` | `/events?after=<seq>&limit=100` | event log for owner verdicts | `200`, `400` invalid query |
| `POST` | `/notify` | a reminder or the morning brief from Central Time core (`notification.py`): `{kind, ref, title, body?, actions?}`, sent once per `ref` (persisted). Opened by the mailbox token or `MAILBOX_NOTIFY_TOKEN`, which opens nothing else. Not an ask: never in `/asks` or `/events`. A reminder's Done/Tomorrow buttons ack in Central Time, then the message is struck through | `201` sent, `200` deduped (`{"id", "deduped": true}`, nothing sent), `400` `{"error": "invalid notification", "details": [...]}`, `502` Telegram send failed (nothing stored; the retry sends) |

Event kinds: `posted`, `acked`, `steps`, `reply`, `resolved`, `declined`,
`reopened` (actor `owner` or `session`, `text` null), `answer` (actor `owner`,
`text` like `{"step":"3","id":"s3","picked":["Staging"]}`, plus `"text"` for the owner's
words on a choice that takes them (1.4.0); the latest per step wins),
`revised` (actor `session`, `text` `{"hash","reason"}`), `accepted` and `problem`
(actor `owner`, `text` `{"id","step","note"}`), `message` (actor `owner`, the text
verbatim), a bug's `posted` and `occurrence` (actor `hook`, `text` `{"count"}`),
`progress` (actor `owner`, `text` `{"tick":[ids],"untick":[ids]}`, ids only; never an
owner act a client shows), `ask_about_question` (actor `owner`, the question) and
`ask_about_answer` (actor `hook`, the answer) (1.14.0; never an owner act a session
reads), and `edit_failed` (actor `daemon`).

Choice objects (1.4.0): a question's or the reply's choice may be
`{"label": "Mix", "text": "What should change?"}` in place of a plain string, a choice
the owner answers with words; `text` is the placeholder of Hook's field for them.
Both keys are required, the label follows the string rules (1-60 characters, one
line, distinct across the list), `text` is 1-100 characters on one line, and such a
choice can't be in a multi question. A step answer carries the words as `text` beside
`picked`; a reply reads `choice: Mix · <words>` (or `choice: Mix` with none). Telegram
buttons show the label. A client sends `X-Hook-Protocol: 6` to read the objects: an
older one (Hook 0.13, protocol 5 or none) gets an ask with a choice object in any
revision as labels only, a rev-1 body with no `revision` or `revisions` (from rev 2 on,
the update stub), and every other ask unchanged.

Documents (1.10.0, behind the `docs` switch): a step's or sub-step's `file`, or a
link's `path`, may carry `doc` `{"sha256": <64 lowercase hex>, "bytes": <1 to 262144>}`,
the fingerprint of the document it points at; the revision hash covers it. The text
travels beside the instructable, as the request's `docs` member
`{"<sha256>": "<text>"}` on `POST /asks`, amend and append (absent or `null` is none).
Each text's key is the SHA-256 of its UTF-8 bytes; a text is 1 to 262,144 bytes, at most
786,432 bytes in all, and is stored by hash with the ask, in the same transaction; a dedupe hit
stores nothing. A `doc` the ask already holds needs no text on a correction or an
append. Errors name a field path or a hash, never the text, and all come back
together as `{"error": "invalid documents", "details": [...]}`: `docs: must be an
object of SHA-256 hash to text`, `docs.<sha256>: does not match its hash`,
`docs.<sha256>: larger than 262144 bytes`, `docs: more than 786432 bytes in all`,
`docs.<sha256>: not used by any step or link`, `steps[0].doc: no document with this
hash`, `steps[0].doc.bytes: does not match the document`, `docs.<sha256>: secret-like
value` and `docs.<sha256>: not text` (a NUL). The 1 MiB request cap still applies
(`413`). While the switch is off, a post with `docs` or any `doc` key, also
`"doc": null`, is `409 {"error": "documents not enabled"}`; one with neither is
unchanged. A correction or an append is `409` only when it has a `docs` member, a
`doc` whose hash the ask does not already hold, or a `"doc": null` where the ask's
current body has none: an ask posted with documents while the switch was on can still
be corrected and added to while it keeps only those. While the switch is on,
`"doc": null` is no document.
The Pond keeps at most 268,435,456 bytes (256 MiB) of document text in all (1.12.0),
the sum of every stored text's bytes, each text counted once whichever asks link it. A
post, correction or append whose new texts (those whose hash is not stored yet) would
take the total over it is `409 {"error": "document store full"}`, checked after the
documents' other errors, and nothing is stored: no ask, revision, document or event,
and normally no Telegram message is sent. It is all or nothing: one text over the
limit refuses the whole request. A text the Pond already holds adds nothing, and a request that sends no text
is never refused for it; a dedupe hit is answered as before. A post is checked before
its Telegram send and again in its transaction, so two requests at once never go over
the limit. In the rare case where two posts race for the last space, the losing post's
message was already sent: it is edited to say the ask was not stored, and its buttons
are removed.
A client reads a text with `GET /asks/{id}/docs/<sha256>` and, once the ask is done
or dismissed, releases them with `POST /asks/{id}/docs/release`. An ask keeps its
documents until it releases them, or until 7 days after the ask is closed, whichever
comes first (the Pond sweeps once at start and nightly after the backup, switch on or
off); a reopened ask keeps its documents while it is open, and closing it again starts
a new 7 days. A text is kept until every ask that linked it released it or expired;
released or expired text stays in the nightly backups until they rotate out (30 days).
Releasing or expiring texts frees their bytes under the limit; an open ask keeps its
documents as long as it is open, and `docs status` shows how many are stored, their
bytes in all and the limit.
Checking a text for secret-like values takes about 0.1 s of CPU per 256 KB of prose.

Long text (1.4.0): messages and problem reports take 8,000 characters. Telegram
sends a keyboard-less message past 4,096 UTF-16 units as several, never cutting an
entity, a tag or a surrogate pair; an ask's message stays under 3,500 and an edit is
never split. Agent clients cut owner text in a session line at 8,000 characters and
name the rest: ` (N more characters: relay status <id> --json)`.

Required before Done (1.3.0): an ask with `required: true` steps or a `done` list
(`steps`, `reply`, `message`) carries `requirements` `{"left": [...], "ready"}` on
`GET /asks/{id}` and on every route that answers one item, never on `GET /asks`.
Telegram hides Done until `ready`, marks a required step " (required)" and adds a
"Before Done: …" line for `done`.
Every event carries `rev` and `auth` (`owner-key`, `token` or `telegram` on owner
events; null when unknown). A `message` is the owner's only with `auth` `owner-key`
or `telegram`: while the owner key is not enforced, any session holding the token can
post one (`auth` `token`), so a client should show that as an unverified message (not
from the owner's key), never as an owner message. A reopened ask is open again, so a repost of the same title
within 24h of its creation still dedupes to it.

On a corrected ask, answers and the owner's Done need the accepted version:
`rev` (and `hash`, when given) must be the current revision and the accepted one,
else `409 {"error": "ask was corrected", "rev", "hash", "acceptedRev"}`. Skip
(declined) and the session's own acts are never bound. Telegram buttons carry the
rev of the message they are on from rev 2 on (`d:<id>:2`); a rev-1 message has the
1.0.0 buttons (`d:<id>`), which a rollback still reads.

Trust model: one owner. Owner acts (`/reply`, `/answers`, `/progress`, an owner `/resolve` or
`/reopen`) need Hook's owner key (`X-Hook-Owner-Key`) once enforcement is on, and
`/accept`, `/problems` and a bug's `POST /asks` always do; the token alone can post, amend with an edit
key once corrections are on, and read. Enforcement is off by default. The regular
token also opens `/notify`, so a holder can put `r:` reminder buttons on the
owner's phone; the notify token opens `/notify` only. Any request body with a
duplicate JSON key is `400 invalid json`.

## Health

`GET /health` is `200` when both are true, else `503`:
- The last successful Telegram poll was less than 120s ago (so a broken bot
  token or a stalled poll loop shows as unhealthy, not silently stuck).
- The SQLite database is readable and writable.

It stays `503` from a cold start until the first `getUpdates` call succeeds —
this is normal for the first couple of minutes after `docker compose up`.

## Backups

A nightly snapshot at 03:20 local time (`TZ` from `.env`) into `backups/`,
keeping the last 30 days. `backups/` is 700, owned by the deploying user —
`prepare-host.sh` sets this before first start and will refuse to run without
it. `backups/mailbox-token` is the mirrored copy of the token from step 5
above; it is never displayed by any command in this document.

## Hook (formerly Relay)

Set `MAILBOX_RELAY_OPEN_BASE` in `.env` to the public Pond link base, for example
`https://pond.example.test/i`. Every ask's Telegram message then gets an
`📋 Open in Hook` row pointing at `<base>/<id>`, and `POST /asks` answers with
`"openUrl": "<base>/<id>"`, on a new ask and on a dedupe hit alike. Unset, no link
row or `openUrl` is added. A loopback base (Hook's local page on a Mac) still
works on that Mac only.

`GET /i/<uuid>` is public: it serves the same fixed HTML for every valid UUID,
without reading an ask or exposing its existence or contents. It tries to open
Hook automatically (on Android the intent link, elsewhere `hook://i/<uuid>`) and
always offers iPhone (`hook://`), Android
(`intent://i/<uuid>#Intent;scheme=hook;package=io.github.xooxoxxo.hook.android;end`)
and Mac (`relay://`) links, plus one "Open in Hook" button that its script points at
the detected device's link (1.12.1). Queries, fragments, extra paths and noncanonical UUID shapes
are refused. The page uses a restrictive CSP with only its fixed launch script and
fixed stylesheet, each allowed by its SHA-256 hash (no `'unsafe-inline'`), and its icon as a fixed `data:` image (`img-src data:`), no external assets, credentials or redirect destination. Both schemes work on
Mac; `relay://` links remain supported.

`MAILBOX_TELEGRAM_MODE=full` is the default and retains Telegram answers, Done,
Skip and text replies. `alert` sends only project, title, urgency and Open in
Hook guidance for asks, including corrections and edits. Its only button is
Open in Hook when a base is configured. Old ask buttons and text replies are
refused with Open in Hook guidance and store no owner action or reply. Ask
session notes and resolution notes stay in Hook; their API events are retained.
Enrollment and owner-key recovery buttons, reminders, capture and household
workflows keep working. An unknown mode refuses startup.

An ask with an `instructable` shows on Telegram as its headline (⚡ when urgency is
`now`), why, and up to 10 numbered steps (`+N more` after that), capped at 3500
characters. Code blocks stay in Hook.

A step (and `verify`) may carry `detail`: longer instructions, up to 1500
characters, multi-line, under the same hidden-character and secret rules as the
other step text (an error names `steps[N].detail`). Hook shows it in full;
Telegram shows it under its step on one line (line breaks become ` / `), in
italics, shortened to about 280 characters with `…`. When the 3500 cap is near,
a step's detail is dropped before the step itself.

A step or sub-step (never `verify`) may carry `fields` (1.13.0, feature `fields`): a
settings table Hook shows under `do`, label on the left, value on the right, with a
Copy button on a row marked `copy`. It is 1 to 20 rows
`{"label": "Name", "value": "requests-ip", "copy": true}`: `label` 1 to 40 characters
on one line and unique within the step (compared exactly), `value` 1 to 500 characters
on one line under the hidden-character rule of `code`, `copy` true or false (absent is
false, and is not filled in). Both go through the secret scan. The revision hash covers
the rows, and a correction that changes one names the step's `fields` in its summary
(`settings` on Telegram). Telegram doesn't show the rows, as it doesn't show code.
Errors name the row: `steps[0].fields: must be a list of 1 to 20 rows`,
`steps[0].fields[1]: must be an object`, `steps[0].fields[1].label: duplicate label`,
`steps[0].fields[1].value: must be a single line`, `steps[0].fields[1].copy: must be
true or false`, `verify.fields: unknown key`. Vectors: `tests/fields-vectors.json`.

An ask may carry `brief` (1.14.0, feature `brief`): the background its session would
have told the owner in chat (the why, the context, the links it used), Markdown,
multi-line, at most 8000 characters, under the prose hidden-character rule and the
secret scan. Hook shows it behind a "Background" disclosure. It is a top-level key of
the body, so the revision hash covers it and no step identity does; a correction that
changes it names `brief` in the summary's `top` (Telegram says `background`). Telegram
doesn't show it. Errors: `brief: longer than 8000 characters`, `brief: must be a
string`, `brief: secret-like value`. Vectors: `tests/brief-vectors.json`.

### Ask about this

`POST /asks/{id}/ask-about` (1.14.0) answers the owner's question about an ask with
Claude Haiku 5.5 (`claude-haiku-5-5`, at most 600 output tokens, no tools), from the
ask's title, why, steps (do, where, detail, settings, command, expect, questions),
the owner's current answers and ticks, and its `brief`. It is on only while the Pond
has an Anthropic API key: `ANTHROPIC_API_KEY` in `.env`, or `ANTHROPIC_API_KEY_FILE`
naming a file inside the container (for example one under the mounted `/backups`,
mode 600). The key is never logged. With a key, `GET /version` lists `askAbout`;
without one the route answers `404`. Limits: 20 questions per ask, 200 per Pond per
UTC day (`429` with a plain `message`); a failed call answers `502` answer
unavailable and stores nothing. The events `ask_about_question` and
`ask_about_answer` are in the ask's history and in `/events`, but no session reader
(`relay wait`, the replies hook, an agent client's replies) counts them as owner acts, so
the session is never woken.

**Privacy:** when the owner asks about an ask, the ask's text and its brief go to
Anthropic's API. If your agents already run on Claude, no new party sees them.
Nothing is sent while nobody asks, and nothing at all without a key.

## Capture and reminders

Optional. With `MAILBOX_CT_URL` and `MAILBOX_CT_TOKEN` set (a Central Time
capture addon token), a text from the owner
that is not a reply to an ask becomes a Central Time item: a title, then an
optional date and time at the end (`today`, `tomorrow`, `mon`..`sun`,
`YYYY-MM-DD`, `D.M`, `H:MM`; a trailing `.` or `,` after them is ignored),
answered under the message with `Added: … · tomorrow 09:00`. Bot commands
(`/…`) and anything that looks like a secret are never sent. Central Time core
posts reminders and the brief to `POST /notify` with `MAILBOX_NOTIFY_TOKEN`; a
reminder's Done and Tomorrow buttons call Central Time back
(`POST /api/reminders/<id>/ack`) with the capture token and then edit the
message. Only the buttons act on a reminder; a text reply to one is ignored.
Notifications live in their own table and never appear in `/asks`, `/events`,
or Hook. With the three keys unset the daemon
behaves exactly as before (`capture: disabled, notify: disabled (tz …)` in the
log). `TZ` must be the zone Central Time runs in, and compose refuses to start
without it; today, tomorrow and a bare time in a capture follow that zone's
wall clock (the daemon resolves `TZ` itself and prints the zone with its
offset). No message text is ever logged.

Bad keys never stop the daemon: `MAILBOX_CT_URL` without `MAILBOX_CT_TOKEN`
(or the reverse) logs a warning and leaves capture off, and a key that is set
but malformed (a non-http URL, a token of the wrong shape) logs one warning
naming the key and disables its feature (capture for the Central Time pair,
`/notify` for the notify token), so the ask channel always starts. Central
Time does the same on its side when `CT_MAILBOX_URL` and `CT_MAILBOX_TOKEN`
are not both set or not well formed. Check the startup line after a change:
`capture: disabled` or `notify: disabled` with a key set means the warning
above it names the key to fix.

Tokens: the regular mailbox token also opens `/notify`, so whoever holds it
can put `r:` reminder buttons on the owner's phone. `MAILBOX_NOTIFY_TOKEN`
opens only `/notify`, nothing else; that is the one Central Time core holds.

Delivery is store-on-success. A notification row is committed only after
Telegram accepts the message, so any failure between Telegram accepting a
message and the row commit means a retry sends it again: a rare double send,
chosen over a reminder that is never sent.

Reaching Central Time: the compose file maps `host.docker.internal` to the
docker bridge address of the host (`host-gateway`), where Central Time
publishes 8787. If a host firewall filters that route, set
`MAILBOX_HOST_GATEWAY=<LAN address of the host>` in `.env` and recreate the
container. Set `TZ` to the same zone as the Central Time install.

## Household mode

`MAILBOX_MEMBERS=owner:<id>,partner:<id>` (lower-case names, Telegram user
ids, the owner included) switches a daemon into household mode. A household
daemon takes no asks, so run it as a second Pond with its own bot token, next
to the asks one and never in place of it:

1. Copy `mailbox/` to a second folder (for example `pond-household/`) and
   write that folder's own `.env` (mode 600) with the second bot's
   `TELEGRAM_BOT_TOKEN`, the same `MAILBOX_OWNER_ID` (for example `12345`),
   `MAILBOX_CONTEXT` (for example `household`), `TZ`, `MAILBOX_MEMBERS`
   (for example `owner:12345,partner:222`), `MAILBOX_CT_URL`,
   `MAILBOX_CT_TOKEN`, `MAILBOX_HOST_PORT` set to a free host port (for
   example `8796`) and `MAILBOX_IMAGE=pond:household`, so building this stack
   never retags the asks stack's `pond:local` image.
2. Run `sudo ./prepare-host.sh` in that folder, then start it under its own
   compose project name:
   `docker compose -p pond-household up --build -d`.
   `-p` overrides the `name: mailbox` in `compose.yaml`, so the second stack
   gets its own container and volume (`pond-household_mailbox-data`) instead
   of replacing the asks stack; its `backups/` is that folder's own. Use the
   same `-p` for every later `docker compose` command in that folder.

A daemon with `MAILBOX_MEMBERS` set needs `MAILBOX_CT_URL` and
`MAILBOX_CT_TOKEN` too, or it refuses to start. In household mode only members' private chats are read; a text or a
captioned photo (JPEG as Telegram sends it, at most 5 MiB, the cap enforced
while downloading) becomes a Central Time household capture
(`POST /api/hh/commands`, command id `tg:<chat>:<message>`, so a redelivered
update replays instead of duplicating), answered with
`In Inbox: … · tomorrow 09:00`. Groups, forwards, bot commands, files sent as
documents and anything that looks like a secret are never sent. `POST /notify`
takes reminders only, needs `to` (a member's Telegram id) and the actions
done, accept or decline; a tap calls `POST /api/hh/deliveries/<id>/ack` as the
tapping member, and only the recipient's tap counts. `POST /asks` answers 403:
a member never receives an owner ask. Notification rows are transport copies
of what Central Time holds and are pruned after 35 days by the nightly backup.
With `MAILBOX_MEMBERS` unset none of this runs and the asks daemon behaves
exactly as before (pinned by `tests/test_asks_golden.py`).

## Using it from Hook

Hook's apps and its `relay` CLI talk to a Pond by URL and token: give them the
Pond's address (behind your TLS proxy) and the token from
`backups/mailbox-token`. A client checks `GET /version` and gates each feature
on its name in `features`. Hook keeps the canonical wire and validation vectors
in its `conformance/` folder; the copies in `mailbox/tests/` must match them byte
for byte (`HOOK_CONFORMANCE_DIR`, see Development).

## Development

Python 3.12 and pytest. From the repository root:

```
python3 -m pytest -q mailbox/tests
```

Set `HOOK_CONFORMANCE_DIR` to a Hook checkout's `conformance/` folder to also
compare the shared vector files with Hook's; unset, that check is skipped. The
`mailbox/tests/gen_*.py` generators rebuild the synthetic vector files.
