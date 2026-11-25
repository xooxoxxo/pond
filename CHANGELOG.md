# Pond changelog

The Pond (the `mailbox` package in this repository) is versioned with semver. `GET /version`
answers `{"service": "pond", "version": "<semver>", "protocol": <n>}`. Hook
releases name the Pond range they need (an npm-style range such as `^1.1.0`),
so a minor or patch release never forces a Hook update. A major release is
only for a breaking change: a removed route, a changed meaning, or required
auth that isn't behind a switch. `protocol` is kept for Hook 0.9.x and its
clients, which compare it; new code reads `version`.

## 1.15.0

Backwards compatible: `protocol` stays 5, and Hook clients that need `^1.5.0`
keep working. No migration: schema 5 is unchanged, no new column or setting (an
answer is still one row of the `events` table). No new dependency. New feature name
`longAnswers` in `features`.

- An owner's answer text takes 8000 characters (1000 before), as a message to the
  session has since 1.4.0: a free-text answer to a step question, and the words
  beside a pick of a choice that takes them. Both stay multi-line, under the note
  rules and the secret scan. One past the cap is refused as before, with the new
  number: `400` `{"error": "invalid answers", "details": ["answers[0].text: longer
  than 8000 characters"]}`, and nothing is stored. A client gates the longer limit
  on `longAnswers`; an older Pond still refuses past 1000.
- Telegram never shows an answer's text: an answer redraws the ask's buttons (✓ on
  the current picks) or, in alert mode, the headline. So a long answer can't take a
  Telegram message past its 4096-character limit; the whole text is in Hook.
- Sessions read a long answer as they read a long message: `relay wait`, the
  replies hook, `relay status` and the agent client's `replies` quote up to 8000
  characters and name the rest (`N more characters: relay status <id> --json`).
  Ask about this reads the counted answers whole.
- `answers-vectors.json`: `answers-text-1000` and `answers-text-1001` are now
  `answers-text-8000` (accepted) and `answers-text-8001` (refused).
  `choice-vectors.json`: `choice-answer-words-too-long` sends 8001 characters (the
  file still names 1.4.0, where choice objects began). The vector files stamped with
  the Pond's release (history, progress, strict-JSON, append, document and phone)
  name 1.15.0.

## 1.14.0

Backwards compatible: `protocol` stays 5, and Hook clients that need `^1.5.0`
keep working. No migration: schema 5 is unchanged, no new column or setting (the
new events are rows of the `events` table). No new dependency: the model is called
with `urllib`, as Telegram is. New feature name `brief` in `features`, and
`askAbout` while the Pond has an Anthropic API key.

- `brief`, a new optional top-level key of an instructable: the background a
  session would have told the owner in chat (the why, the context, the links it
  used), Markdown, multi-line, at most 8000 characters, under the prose
  hidden-character rule and the secret scan (`brief: longer than 8000
  characters`, `brief: must be a string`, `brief: contains hidden or control
  characters (U+…)`, `brief: secret-like value`). Its error comes after `warning`
  and before `urgency`. It is stored with the ask in the revision body and served
  in the ask detail like every other key; the revision hash covers it, and no step
  identity does (ticks and answers never depend on it). It counts toward the
  32768-byte limit of the whole instructable. A correction can change, add or
  remove it: the summary names `brief` in `top`, as it names `why` (Telegram's
  Corrected reply says `background`). An append keeps it.
- `POST /asks/{id}/ask-about` `{"question"}`: the owner asks about an ask and
  Claude Haiku 5.5 (`claude-haiku-5-5`, the Anthropic Messages API, at most 600
  output tokens, no tools, thinking off, effort low) answers from the ask (title,
  why, warning, every step's do, where, warning, detail, link, settings, command,
  expect and question, the owner's current answers and ticks, verify, the reply
  prompt and links) and its brief. The system prompt says to answer only from that
  context, plainly and briefly; to say so and suggest "Ask the session" when the
  answer isn't there; never to invent a command or a value; and never to ask for a
  secret. Auth and the owner key as for an owner message. The question is 1 to
  1000 characters, multi-line, under the note rules (`400` `{"error": "invalid
  question", "details": [...]}`, so a secret is never sent on). `200` `{"answer",
  "events": [question, answer]}`.
- The question is stored as event `ask_about_question` (actor `owner`, `auth` as
  sent) and the answer as `ask_about_answer` (actor `hook`), both bound to the
  current revision, in one transaction and only after the model answered. On any
  failure nothing is stored, so a failed call never counts toward a limit. The
  session is not told: `relay wait`, the replies hook, `relay status`,
  the agent client's `replies` and the Pond's own owner-attention list read named owner
  kinds only, and these two are not among them. Neither counts as the `message` a
  `done: ["message"]` ask needs.
- Limits: 20 questions per ask and 200 per Pond per UTC day, `429` `{"error":
  "too many questions"}` or `{"error": "daily limit reached"}`, each with a plain
  `message`. One question is answered at a time. `409` ask is closed. A model that
  fails, refuses or gives no text answers `502` `{"error": "answer unavailable"}`.
- The key: `ANTHROPIC_API_KEY`, or a file named by `ANTHROPIC_API_KEY_FILE`. It is
  never logged, echoed or stored. Without one (or with an unreadable file, which
  warns once naming the variable) the route answers `404` as an unknown route does
  and `features` has no `askAbout`.
- Privacy: the ask's text and its brief go to Anthropic's API when the owner asks
  about it. Nothing else does.
- `brief-vectors.json` (generated by `gen_brief_vectors.py`) holds the validator
  cases and the canonical, hash and amend cases with `brief`. Every vector file
  names 1.14.0.

## 1.13.0

Backwards compatible: `protocol` stays 5, and Hook clients that need `^1.5.0`
keep working. No migration: schema 5 is unchanged, no new column or setting. No
new dependency. New feature name `fields` in `features`.

- A step or sub-step may carry `fields`, a settings table: 1 to 20 rows
  `{"label", "value", "copy"?}`. `label` is 1 to 40 characters on one line and
  unique within the step; `value` is 1 to 500 characters on one line, under the
  hidden-character rule of `code` (no ZWJ or variation selector); `copy` is true
  or false, absent means false and is not filled in. `verify` can't have it
  (`verify.fields: unknown key`). Labels and values go through the secret scan.
- The rows are part of the step: the canonical body and the revision hash cover
  them, and a correction that changes, adds or removes a row names `fields` in the
  step's `changed` list. Telegram's Corrected reply calls it `settings`; Telegram
  doesn't show the rows themselves, as it doesn't show code.
- Errors, in this order within a row: an unknown key, then `label` (`required`,
  `must not be empty`, `longer than 40 characters`, `must be a single line`,
  hidden characters, `duplicate label`), then `value` (the same, with 500), then
  `copy: must be true or false`. A step's `fields` errors come after its `url`
  and before its `file`.
- `fields-vectors.json` (generated by `gen_fields_vectors.py`) holds the shared
  validator cases and the canonical, hash and amend cases with `fields`, in the
  shapes of `revision-vectors.json`, which stays as it was (Hook copies it into
  its Swift fixtures). Every vector file names 1.13.0.

## 1.12.1

Backwards compatible: `protocol` stays 5, and Hook clients that need `^1.5.0`
keep working. No migration: schema 5 is unchanged. No new feature name in
`features`. No new dependency.

- The public `GET /i/<uuid>` launch page is restyled as a quiet card: the Hook
  app icon (144 px, about 10 KB, a fixed `data:` URI kept in `link_icon.py`), "Opening Hook…" with a small spinner (still under
  `prefers-reduced-motion`), one "Open in Hook" button aimed at the detected
  device, small iPhone, Android and Mac links, and the line "Needs Hook installed
  and connected to your Pond." It follows light and dark mode with system fonts
  only. The page is still the same for every valid UUID and never reads an ask.
- The fixed stylesheet is allowed by its hash: the CSP gains
  `style-src 'sha256-…'` and `img-src data:` for the icon, still with no
  `'unsafe-inline'` and no external source.
- The fixed script now also points the button at the device's link (Android:
  the intent link, iPhone or iPad: `hook://`, anything else: `relay://`). The
  redirect is unchanged: Android opens the intent link and every other browser,
  Mac included, opens `hook://i/<uuid>`.
- The device links now read iPhone, Android and Mac (ids `iphone`, `android`,
  and the new `mac`); their targets are unchanged.

## 1.12.0

Backwards compatible: `protocol` stays 5, and Hook clients that need `^1.5.0`
keep working. No migration: schema 5 is unchanged, no new column or setting. No
new feature name in `features`. No new dependency.

- A total limit on stored documents: the Pond keeps at most 268,435,456 bytes
  (256 MiB) of document text, the sum of every stored text's bytes. Each text is
  stored once by hash and counts once, whichever asks link it. Before, there was
  no total cap, and a session holding the token could fill the disk.
- A post, correction or append whose new texts (those whose hash is not stored
  yet) would take the total over the limit is `409` `{"error": "document store full"}`,
  after every earlier check (the request's own, the instructable, the
  `docs` switch, `400` invalid documents). Nothing is stored: no ask, revision,
  document, link or event, and normally no Telegram message is sent (see the race
  below). It is all or nothing: one text over
  the limit refuses the whole request. A text the Pond already holds adds
  nothing, a request that sends no text is never refused for it, and a dedupe hit
  is answered as before. Bugs are unchanged (they take no documents).
- The check runs inside the transaction that stores the texts, so two requests at
  once never go over the limit. A post is also checked before its Telegram send.
  In the rare case where two posts race for the last space, the losing post's
  message was already sent: it is edited to "This ask was not stored: the
  document store is full." and its buttons are removed. The answer is still the
  `409`, also when that edit fails.
- Releasing an ask's documents, or the 7-day expiry after it closes, frees their
  bytes, and a refused post then fits.
- `python3 -m mailbox.admin docs status` prints the limit beside the stored
  bytes: `docs stored: 3 (1024 of 268435456 bytes)`.
- `doc-wire-vectors.json` gains the case `doc-post-store-full` (recorded with a
  small limit), and every vector file names 1.12.0.

## 1.11.0

Backwards compatible: `protocol` stays 5, and Hook clients that need `^1.5.0`
keep working. Follows 1.10.0 (documents, schema 4); merges after 1.10.0. Schema 5:
one migration step adds the column `last_used` to the side table `owner_keys`
(read by column name, so 1.10.0 and older code ignore it) and the side table
`owner_revoke_requests`. No column on `items` or `events`, no backfill. No new
dependency.

- The owner key limit is 10 approved keys (4 before).
- `GET /owner` lists each key's `lastUsed` beside `created` (ISO 8601; null
  until a request authenticates with the key, then stamped at most once an hour
  per key) and the limit as `maxKeys`. Key hashes are still never returned. It
  still needs only the Pond token, so a Mac without a working key can show the
  device list.
- `POST /owner/revoke-request {"id"}`, the Pond token only: asks the owner on
  Telegram to revoke an approved key. Telegram shows "Revoke Hook on <label>
  (set up <date>)? Code <code>" with Revoke and Keep; the code is
  `owner.revoke_code(request id, key id)`, 6 digits. Nothing changes until the
  owner taps Revoke, which revokes the key and sends the usual "can no longer
  act as you" line. 10 minutes; one pending request at a time.
  `201` `{"id", "code", "expires", "status": "pending"}`; `400` missing fields;
  `404` unknown key; `409` already revoked, not approved, or revoke request
  pending; `502` telegram send failed. An owner key header is ignored.
- `GET /owner/revoke-request/{id}` → `{"id", "status"}`: pending, approved,
  rejected or expired (applied on read).
- Features: `ownerRevokeRequest` and `ownerKeyDates`, so Hook can tell an
  older Pond.
- The phone vectors (`phone-setup-vectors.json` and the others) are regenerated:
  `GET /owner` now carries `lastUsed` and `maxKeys`, and every file names 1.11.0.

## 1.10.0

Backwards compatible: `protocol` stays 5, and Hook clients that need `^1.5.0`
keep working. Schema 4: one migration step adds the side tables `docs` (each
text once, by hash) and `ask_docs` (which ask links which text, and whether it
released it); no column on `items` or `events`, no backfill. A Pond 1.9.0 still
opens a migrated database and never reads the new tables. No new dependency.

- An ask can carry a checked copy of a Markdown or text document. A step's or
  sub-step's `file`, or a link's `path`, may carry `doc` `{"sha256", "bytes"}`,
  the fingerprint of the document it points at (64 lowercase hex, 1 to 262,144
  bytes); the revision hash covers it.
- The text travels beside the instructable, in the request's `docs` member
  `{"<sha256>": "<text>"}` on `POST /asks`, amend and append. Each text's key is
  the SHA-256 of its UTF-8 bytes; a text is 1 to 262,144 bytes, a request carries
  at most 786,432 bytes of text in all, and the 1 MiB request cap still applies.
  The texts are stored with the ask in the same transaction. A `doc` the ask
  already holds needs no text on a correction or an append. Errors name a field
  path or a hash, never the text: `400` `{"error": "invalid documents", "details":
  [...]}`. A Hook bug takes no documents.
- Checking a document for secret-like values takes about 0.1 s of CPU per 256 KB of
  prose. A text another check refuses (it is not used, or the request is over its
  total) is not scanned.
- `python3 -m mailbox.admin docs on|off|status`: documents are off by default.
  While off, a post with `docs` or any `doc` key, also `"doc": null`, is `409`
  `{"error": "documents not enabled"}`; a correction or an append is refused only
  when it has `docs` or a `doc` the ask does not already hold, so an ask posted with
  documents can still be corrected and added to. `GET /version` lists `docs` in
  `features` only while it is on. Turning it off keeps every document already
  stored, and reading and releasing still work.
- `GET /asks/{id}/docs/{sha256}` (the token, as for `GET /asks/{id}`): `200` with
  the exact stored bytes, `Content-Type: text/plain; charset=utf-8`,
  `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`; `404` `{"error":
  "not found"}` for an unknown ask, a malformed hash or one the ask never linked;
  `410` `{"error": "document no longer kept"}` once the ask released it, also when
  another ask still keeps the same text.
- `POST /asks/{id}/docs/release` (the token only, so an agent that took the work
  over can release; the body is ignored): `200` `{"released": n}`, the documents
  this call released, `0` on a second call; `409` `{"error": "ask is open"}`, and
  nothing is deleted; `404` for an unknown ask. It deletes the texts the ask linked
  that no other ask still keeps, and writes no event.
- Documents are kept until their ask releases them, or until 7 days after the ask
  is closed, whichever comes first; a reopened ask keeps its documents while it is
  open, and closing it again starts a new 7 days. The expiry runs once at start and
  nightly after the backup, switch on or off, and writes no event. Released or
  expired text stays in the Pond's nightly backups until they rotate out (30 days).
  There is no total cap on stored documents: an open ask keeps its documents as long
  as it is open, and `docs status` shows how many are stored and their bytes in all.
- A released ask that is reopened and corrected can keep its `doc` without the
  text (the read answers `410`), or send the text again, which keeps it again.
- No document text is in a log line, an error, a Telegram message, `/health`,
  `/i/<id>` or any list, detail or events answer. Telegram still shows a file as
  "📄 plan.md (on the Mac)".
- Transcript for the phones: `tests/doc-wire-vectors.json`.

## 1.9.0

Backwards compatible: `protocol` stays 5, and Hook clients that need `^1.5.0`
keep working. Schema 3: one migration step adds the side table `revision_meta`
(no column on `items` or `events`, no backfill); 1.8.0 and older code ignore it.
No new dependency.

- `POST /asks/{id}/append {editKey, baseRev, baseHash, note, steps}`: the agent
  that posted an ask adds 1 to 10 steps after its last step. `note` is 1 to 200
  characters on one line and becomes the revision's `reason`; the hash formula
  doesn't change. Steps without an id get `r<rev>-<n>`. The whole result passes
  the post rules (20 steps, 60 steps and sub-steps, 20 questions, 32 KB, ids
  unique across every revision, the secret scan). Error details name the
  request's steps (`steps[0].id: used before`, `note: too long`); a limit on the
  whole result names the result (`steps: must be a list of 1 to 20 steps`).
  `200` `{"item", "revision": {rev, hash, kind, added: [{id, path}]}, "openUrl"?}`;
  `409` appends not enabled, ask is closed, stale revision or append limit
  reached; `403` not editable or wrong edit key; `429` too soon (`retryAfter`);
  `400` invalid steps with `details`.
- Appends have their own limit, 10 per ask, counted apart from the 10
  corrections, and wait 15 s after a revision of any kind (an amend keeps 60 s).
  A revision is stored with its kind in `revision_meta`; a revision without a
  row is a correction. A revision read with protocol 5 carries `"kind": "append"`
  for an append; a correction keeps its bytes.
- When the owner had accepted the current revision, the Pond accepts the append
  with it (`accepts.auth` `append`, an `accepted` event by `daemon`), so the
  version in force moves and Telegram, answers and Done follow it. An append
  on top of an unaccepted correction leaves the version in force alone. An amend
  that only adds steps is stored and advanced the same way; one that fails the
  append gates (appends off, 10 appends reached, more than 10 steps) is refused
  with the append route's error, and nothing is stored.
- `GET /version` lists `appendSteps` in `features`.
- `python3 -m mailbox.admin appends on|off|status`: appends are on by default;
  off refuses every append with 409 appends not enabled. Corrections keep their
  own switch.
- The owner's Done bound to a rev that steps were added after is refused with
  409 `{"error": "steps_added", "rev", "hash"}`; answers and ticks bound to it
  still count when their steps are unchanged. Skip is never refused.
- A step question may say `"then": "steps"` (the only value; anything else is
  `steps[i].question.then: must be "steps"`, and null counts as absent). After the
  owner answers it, the owner's Done waits for the session's next revision, at
  most 10 minutes from the answer: 409 `{"error": "waiting_for_steps", "until",
  "path"}`, a `stepWait` `{id, path, until}` on the ask's detail, and no Done on
  Telegram. A changed answer starts the 10 minutes again; the same answer sent
  again doesn't. The poll loop brings Done back on Telegram once a wait ends,
  including waits that ended while the Pond was down. Answers are stamped on the Pond's clock.
- Telegram: an append adds "Steps added: see below" to the ask's newest message,
  which keeps its question buttons and loses Done and Skip, and sends the added
  steps as a reply ("➕ 2 steps added · <note>") with the keyboard of the new rev
  from the first added step on. An answer tapped on the old message redraws its
  buttons too, so it shows the ✓. Alert mode only removes the old buttons and
  replies with the alert and "Steps added". The reply is silent when the owner
  acted on the ask in the last 2 minutes (the Pond's own events don't count). An
  amend stored as an append is announced the same way. A Telegram failure is an
  `edit_failed` event and never fails the append.
- `tests/append-wire-vectors.json`, written by `tests/gen_append_wire.py`: the
  wire transcript of a `then: "steps"` answer, the wait, the append and the acts
  around it. Hook keeps a copy as `conformance/wire/append-wire-vectors.json`.

## 1.8.0

Backwards compatible: `protocol` stays 5, and Hook clients that need `^1.5.0`
keep working. No migration or new dependency.

- `GET /version` adds `features`, the list of feature names this Pond has:
  `answers`, `blocking`, `optional`, `fileLinks`, `corrections`, `bugs`,
  `messages`, `requirements`, `longText`, `sharedProgress`, `askWarning`.
  A client gates a new feature on its name in this list, never on the version
  number, so the order Pond releases merge in doesn't change client code. A
  Pond without the key (1.7.0 and older) has none of the features that are
  only read from the list. Nothing else changes.

## 1.7.0

Backwards compatible: `protocol` stays 5, and Hook clients that need `^1.5.0`
keep working. No migration or new dependency.

- The public launch page at `GET /i/<uuid>` can now open Hook on Android. It
  adds an "Open on Android" link,
  `intent://i/<uuid>#Intent;scheme=hook;package=io.github.xooxoxxo.hook.android;end`,
  and its fixed script opens that link when the browser says it is Android.
  Every other browser opens `hook://i/<uuid>` as before. The iPhone and Mac
  links stay, the page still never reads an ask, and nothing else changes.

## 1.6.0

- Accept ask-wide warnings of up to 300 characters and include their changes in revision summaries. Show the caution before Telegram steps. Protocol remains 5.

## 1.5.0

Backwards compatible: `protocol` stays 5; choice-object clients still send
`X-Hook-Protocol: 6`. No migration or new dependency.

- Public `GET /i/<uuid>` serves a fixed, content-free Hook launch page, with
  iPhone and Mac scheme links and a restrictive CSP. It validates an exact UUID
  route, refuses queries and fragments, and never looks up an ask.
- `MAILBOX_TELEGRAM_MODE` accepts `full` (default) or `alert`. Alert asks show
  project, title, urgency and Open in Hook only, on posts, corrections and
  redraws. Old ask callbacks and replies cannot mutate asks; enrollment and
  owner-key recovery, reminders, capture and household behavior remain available.
  Session notes and resolution notes remain stored but stay out of alert messages.
- Shared progress deltas from devices are applied atomically under the store
  lock. Requirements and current revision bindings are preserved.
- Enrollment messages and docs refer to devices. Keep Telegram full mode for
  two weeks after the first successful daily-use phone install; the owner records
  the start/switch dates and deploys alert mode later. Deployment remains unchanged.
  Real Telegram browser handoff is still a rollout device check.

## 1.4.0

Backwards compatible: every 1.3.0 client keeps working. `protocol` stays 5.

- Long text: an owner message (`POST /asks/{id}/reply` with `kind: message`)
  and a problem report (`POST /asks/{id}/problems`) take 8000 characters
  (1000 and 300 before). Past that: `400 {"error": "invalid message",
  "details": ["text: too long"]}` and `400 {"error": "invalid problem",
  "details": ["note: too long"]}`. A problem note is now multi-line, so a
  pasted traceback keeps its lines; the hidden-character and secret rules
  still apply. Any other short note keeps 300.
- The Telegram split: a keyboard-less send (a reply in thread, a plain
  message) past 4096 UTF-16 units goes out as several messages, each at most
  4096, cut at the last newline, else the last space, else as late as it fits,
  never inside an entity (`&amp;`), a tag or a surrogate pair. A tag open at a
  cut is closed and reopened in the next part, and every part replies to the
  same message. An ask's message (under 3500) and an edit are never split.
  Messages and problem reports still never go to Telegram.
- Choice objects: a choice in a question or the reply may be
  `{"label": "Mix", "text": "What should change?"}`, a choice the owner
  answers with words; `text` is the placeholder of Hook's field. Plain strings
  are unchanged. Refused, with field paths: `choices[1]: must be a string or
  {label, text}` (also a number, which read "must be a string" in 1.3.0),
  `choices[1].x: unknown key`, `choices[1].label: required` or the string
  rules (`longer than 60 characters`, `must be a single line`, …),
  `choices[1].text: required`, `longer than 100 characters` or `must be a
  single line`, `choices[1]: duplicate choice` (a label used twice, string or
  object; a reply list of plain strings keeps no duplicate rule), and
  `choices[1]: a choice with text can't be in a multi question`. A correction
  that changes a choice's text changes the question (the summary names it).
  Telegram buttons show and record the label.
- Words on an answer: `POST /asks/{id}/answers` takes `text` beside a single
  pick of a choice with words, `{"step": "2", "picked": ["Mix"], "text":
  "…"}` (1 to 1000 characters, multi-line, the secret rule), stored as
  `{"step","id","picked","text"}`. Words on any other pick: `answers[0].text:
  this choice takes no words`; on a question with no word choice the 1.3.0
  `this question takes picks` stays. A pick without words is as before.
- The reply wire: a reply choice with words is the reply text
  `choice: <label> · <text>` (U+00B7 with a space each side), or
  `choice: <label>` with none. The Pond stores it as any reply.
- `X-Hook-Protocol: 6`: a client that sends it reads choice objects. An older
  client (Hook 0.13 and its clients send 5; none at all is 0) gets an ask
  with a choice object in any revision as labels only: a rev-1 body whose
  `choices` are the labels, with no `revision` or `revisions`, or from rev 2
  on the update stub. That is on `GET /asks`, `GET /asks/{id}` and every
  answer that carries the item. Every other ask is unchanged for every
  client, and its acts (an answer by label, a reply, Done) work as before.
- The agent client: `post` and `amend` refuse a choice object below Pond 1.4.0
  with the words of Hook's `relay post` ("Your Pond (1.3.0) is older than
  `choices[].text` needs (Pond 1.4.0 or newer). Update the Pond, or use plain
  string choices and say what to write in the prompt."), before anything is
  sent and in one `/version` read; requests send `X-Hook-Protocol: 6`; an
  answer with words prints as `answer 2: "Mix · <words>"`; owner text in a
  session line is cut at 8000 characters (300 before) with
  ` (N more characters: relay status <id> --json)` after the quote; and the
  lint flags a string choice like "Mix (reply what)" (the shared lint vectors
  gain `choice-*` cases).
- `choice-vectors.json` and `limits-vectors.json`
  (`python3 -m mailbox.tests.gen_choice_vectors`) record the wire for Hook.
  `message-vectors.json`'s too-long case is now 8001 characters, and
  `questions-vectors.json`'s number choice reads "must be a string or
  {label, text}".
- No migration: the schema stays 2.

## 1.3.0

Backwards compatible: every 1.2.0 client keeps working. `protocol` stays 5.

- Required before Done: `required: true` on a step or sub-step, and the
  ask-level `done` list of 1 to 3 distinct `"steps"` (every non-optional
  top-level step), `"reply"` (the reply prompt answered) and `"message"` (the
  owner messaged the session). The validator refuses, with field paths:
  `required` that isn't a bool, beside `optional`, inside an optional parent,
  or on a parent with no question whose sub-steps are all optional; and a
  `done` that isn't such a list, an unknown or duplicate entry, `reply`
  without a reply prompt, or `steps` when every step is optional. A
  correction may add, drop or change both, and toggling `required` keeps the
  owner's ticks and answers; the change summary names it (`required` in a
  step's fields, `done` in `top`).
- `POST /asks/{id}/progress` `{"tick": [ids], "untick": [ids], "rev", "hash"}`:
  the owner's ticks, which live in Hook, reach the Pond as deltas, so two
  Macs merge. Checked in this order: `401`, `404`, `400 invalid json` or
  `missing fields`, the owner key (as answers), `409 ask is closed`, `409 ask
  was corrected`, `400 {"error": "invalid progress", "details": [...]}`
  (`tick: must be a list of step ids`, `tick[2]: unknown step`, `untick[0]:
  also in tick`, `tick: more than 60 ids`). `200 {"item"}` with
  `requirements`. A tick counts on the accepted revision only while its
  step's own fields (all but id, sub-steps and `required`) are unchanged.
- The owner's Done waits: `POST /asks/{id}/resolve` with `verdict: done` and
  `actor: owner` answers `409 {"error": "requirements_unmet", "left":
  [{"kind": "step", "id": "s2", "path": "2"}, {"kind": "reply"}, {"kind":
  "message"}], "details": ["step 2", "the reply", "a message"]}` while
  anything is left, after the revision check (`ask was corrected` still comes
  first). `details` makes a Hook 0.12 Notice read "requirements_unmet: step
  2". Skip and the session's own Done are never gated.
- `requirements` `{"left": [...], "ready": bool}` on an item, only on
  `GET /asks/{id}` and on the routes that answer one item (resolve, reply,
  answers, progress, accept, problems, reopen), never on `GET /asks`, and
  absent on an ask without requirements.
- `progress` events: actor `owner`, the bound `rev`, `auth`, and `text`
  `{"tick":[sorted ids],"untick":[sorted ids]}`, ids only. Written only when
  a delta changes the ticked set of an ask with requirements. Not an owner
  act: no client shows it.
- Telegram hides Done while anything is left (Skip stays) and redraws the
  keyboard when that flips after a progress, a reply or a message. A stale
  Done tap toasts "Finish it in Hook first"; a "done …" text reply is kept as
  a reply and answered in thread, never closing the ask. A step with its own
  `required` ends with " (required)", and a `done` list adds a "Before Done:
  every step, a reply, a message from Hook" line.
- The agent client: `post` (and `amend`) refuse `required` and `done` below
  Pond 1.3.0 with the words of Hook's `relay post`, before anything is sent;
  `status` prints "Left before Done: step 2, the reply, a message" or "Ready
  for Done"; its lint warns about requirements that can never be met (the
  shared lint vectors gain `required-*` and `done-*` cases); `wait`,
  `replies` and `status` never treat `progress` as an owner act.
- Recorded vectors: `requirements-vectors.json` (post, progress tick,
  no-op and invalid, the 409, the session's Done, the owner's Skip, and
  `requirements` left and ready).
- No migration: progress is an event row, so the schema stays 2 and 1.2.0
  code still runs on the same database.

## 1.2.0

Backwards compatible: every 1.1.0 client keeps working. `protocol` stays 5.

- Hook bugs: `POST /asks` takes `kind: "bug"` with a `signature` (12
  lowercase hex). A bug needs the owner key always, an `instructable` and the
  right `context`, and is never sent to Telegram (`tg_message_id` stays null;
  resolve, reopen and the keyboard redraw send nothing for it). It is deduped
  by signature, not by title: while a bug with that signature is open, a
  repeat adds an occurrence (the count, last seen and the excerpt from the
  first step's `code` move, plus an `occurrence` event by actor `hook`) and
  answers `200 {"item", "deduped": true, "occurrences": n}`; otherwise `201`
  with `"occurrences": 1`. A closed bug's signature files a new bug.
  Errors: `400 invalid kind`, `400 invalid signature`, `403 owner key
  required`. The plain-ask 24-hour title dedupe is unchanged.
- A bug's item carries `"kind": "bug"` and `"bug": {signature, count,
  firstSeen, lastSeen}`; a plain ask has neither key, so a 1.1.0 client
  reads a bug as an ordinary ask with extra keys.
- `GET /asks?kind=bug|ask` filters bugs in or out; any other kind is `400
  invalid query`. Without `kind` the list is the same as before.
- Messages: `POST /asks/{id}/reply` takes `kind`: absent or `"reply"` (as
  before) or `"message"`, anything else `400 invalid kind`. A message is the
  owner talking to the posting session, never an answer: event `message`,
  actor `owner`, the text verbatim (multi-line), `rev` and `auth` recorded,
  Telegram never touched. 1 to 1,000 characters with the note rules (`400
  invalid message` with field paths only, a secret is never echoed), `409
  ask is closed`, and at most 50 per ask (`409 too many messages`).
- The agent client: a message reaches only the posting session (`↳ owner
  message, quoted: "…"`, one line), `wait` wakes on one, and `list --kind
  bug` lists open bugs with their count. `done <id> "<note>"` resolves a bug.
  Only a message with `auth` `owner-key` or `telegram` prints as the owner's;
  any other (`token`, or none) prints `↳ unverified message (not from the
  owner's key): "…"`, since with the owner key not enforced any
  session holding the Pond token can post one.
- The migration to schema 2 is additive: one side table, `bugs`, and no
  column on `items` or `events`, so 1.1.0 code still runs on it.

## 1.1.0

Backwards compatible: every 1.0.0 client keeps working. `protocol` is 5.

- Step ids: every step and sub-step has an `id` (`s1`, `s3.1`, assigned at
  post unless given for every step). An id may not read as a step
  number (`2`, `3.1`), since `step` in an answer or a problem may be either.
- Revisions: every ask with an instructable has an append-only, hashed
  revision history, revision 1 included. Clients that send
  `X-Hook-Protocol: 5` read the current body, `revision` and, on
  `GET /asks/{id}`, every revision. Other clients get a corrected ask as a
  stub that tells the owner to update Hook.
- `POST /asks` takes an optional `editKeyHash`; the response carries
  `revision` and `editable`.
- `POST /asks/{id}/amend`: the poster, holding the edit key, corrects an
  open ask in place. Refused with `409 corrections not enabled` until the
  `corrections_enabled` setting is turned on (`python3 -m mailbox.admin
  corrections on`).
- `POST /asks/{id}/accept` and `POST /asks/{id}/problems`, both with the
  owner key always. Answers and the owner's Done are bound to the accepted
  version (`409 ask was corrected`).
- The owner key: `GET /owner`, `POST /owner/enroll`, `GET /owner/enroll/{id}`,
  `POST /owner/require`, `POST /owner/revoke`. Enrollment is approved on
  Telegram. The enforcement switch is off by default; turning it off again
  needs a Telegram tap.
- Events carry `rev` and `auth`. Telegram buttons on a corrected ask carry the
  rev they act on (`d:<id>:<rev>`); a revision-1 keyboard is the 1.0.0 one, so
  its buttons keep working after a rollback.
- Request bodies with a duplicate JSON key are `400 invalid json`.
- `/steps` text is checked like a note and sent as a plain session note.
- The migration is additive (side tables only); 1.0.0 code still runs on a
  migrated database.

## 1.0.0

What was protocol level 4: the routes and keys Hook 0.9.x uses. Asks,
events, resolve, reply, reopen; step questions and `POST /asks/{id}/answers`;
`blocking` and `warning`; `optional`, `file` on a step and `path` on a link;
`GET /version`; `/notify`. Released as `2026.10.27.1`.
