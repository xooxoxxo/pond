"""Telegram Bot API client via urllib (standard library only)."""
import html
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from mailbox.answers import questions as _questions
from mailbox.instructable import choice_labels

# An ask carrying an instructable is capped well under Telegram's 4096, which
# leaves room for the ack prefix. The full instructable is always in Hook.
MAX_ASK_TEXT = 3500
MAX_RENDERED_STEPS = 10
# A step's `detail` (up to 1500 chars) is shortened to this under its step.
MAX_RENDERED_DETAIL = 280
# The "Before Done:" line's words per `done` value, in the order they are named.
BEFORE_DONE_PARTS = (("steps", "every step"), ("reply", "a reply"), ("message", "a message from Hook"))
DETAIL_MARK = "   <i>"          # starts a step's detail line, and no other line of a block
BLOCKING_FALLBACK = "Do this before the next steps"
# Single-pick questions shown as a row of buttons each, in document order; the
# rest are answered in Hook ("Answer N more in Hook").
MAX_QUESTION_ROWS = 4
# Telegram's cap on one message, in UTF-16 code units. A keyboard-less send past it
# goes out as several messages (Pond 1.4.0); an ask (under MAX_ASK_TEXT) and an
# edit never are.
TELEGRAM_LIMIT = 4096
_ENTITY = re.compile(r"&#?[A-Za-z0-9]{1,10};")
_TAG = re.compile(r"<[^<>]*>")
# Sent on every sendMessage / editMessageText: a URL in an ask never unfurls
# into a preview card the sender controls.
NO_LINK_PREVIEW = {"is_disabled": True}


class TelegramError(Exception):
    """Telegram API error."""
    pass


class FileTooLarge(TelegramError):
    """A file download went past the caller's byte cap."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A file URL carries the bot token in its path: never follow a redirect with it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_FILE_OPENER = urllib.request.build_opener(_NoRedirect)


class _RateLimited(Exception):
    """Internal signal: Telegram returned HTTP 429. Carries the parsed retry_after."""

    def __init__(self, retry_after: int):
        super().__init__(f"rate_limited:{retry_after}")
        self.retry_after = retry_after


def _parse_retry_after(body: dict[str, Any], headers: Any) -> int:
    """Telegram's requested backoff: body.parameters.retry_after, else the
    Retry-After header, else 1 second."""
    parameters = body.get("parameters") if isinstance(body, dict) else None
    if isinstance(parameters, dict) and "retry_after" in parameters:
        try:
            return int(parameters["retry_after"])
        except (TypeError, ValueError):
            pass
    header_value = headers.get("Retry-After") if headers is not None else None
    if header_value is not None:
        try:
            return int(header_value)
        except (TypeError, ValueError):
            pass
    return 1


class TelegramClient:
    """Outbound long-polling Telegram bot over urllib."""

    enabled = True

    def __init__(self, bot_token: str, owner_id: int):
        self.bot_token = bot_token
        self.owner_id = owner_id
        self.api_url = f"https://api.telegram.org/bot{bot_token}/"

    def _call(self, method: str, *, http_timeout: int = 20, **params) -> dict[str, Any]:
        """Make a Telegram Bot API call. On HTTP 429, sleeps the server-requested
        retry_after (capped at 30s) and retries once; a second 429 raises.

        `http_timeout` bounds only the local socket read and is never sent to
        Telegram. A Bot API "timeout" field (e.g. getUpdates' long-poll duration)
        is a normal form parameter, passed like any other via **params.
        """
        # Build form data, handling list/dict values as JSON
        form_data = {}
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, (list, dict)):
                form_data[k] = json.dumps(v)
            elif isinstance(v, bool):
                form_data[k] = "true" if v else "false"
            else:
                form_data[k] = str(v)

        encoded = urllib.parse.urlencode(form_data).encode("utf-8")

        def attempt() -> dict[str, Any]:
            req = urllib.request.Request(
                self.api_url + method,
                data=encoded,
                headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
            )
            try:
                with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    err_body = json.loads(raw) if raw else {}
                except (ValueError, TypeError):
                    err_body = {}
                if e.code == 429:
                    raise _RateLimited(_parse_retry_after(err_body, e.headers)) from e
                description = (
                    (err_body.get("description") if isinstance(err_body, dict) else None)
                    or getattr(e, "reason", None)
                    or str(e.code)
                )
                raise TelegramError(f"{method} {e.code}: {description}") from e
            except urllib.error.URLError as e:
                raise TelegramError(f"{method} network error") from e

        try:
            body = attempt()
        except _RateLimited as first:
            time.sleep(min(first.retry_after, 30))
            try:
                body = attempt()
            except _RateLimited as second:
                raise TelegramError(
                    f"{method} 429: rate limited, retry_after={second.retry_after}"
                ) from second

        if not body.get("ok"):
            err = body.get("description", "unknown error")
            raise TelegramError(f"{method}: {err}")

        return body.get("result", {})

    def send_message(
        self, chat_id: int, text: str, reply_markup: Optional[dict] = None, *,
        disable_notification: bool = False, reply_to: Optional[int] = None,
    ) -> dict[str, Any]:
        """Send a message; optionally with an inline keyboard, silently
        (`disable_notification`) or as a reply to another message (`reply_to`).
        A field left at its default isn't sent."""
        extra: dict[str, Any] = {}
        if reply_to is not None:
            extra["reply_to_message_id"] = reply_to
        if disable_notification:
            extra["disable_notification"] = True
        # A keyboard belongs to one message: only a keyboard-less send is split.
        parts = [text] if reply_markup is not None else self.split_html(text)
        results = [self._call(
            "sendMessage",
            chat_id=chat_id,
            text=part,
            parse_mode="HTML",
            link_preview_options=NO_LINK_PREVIEW,
            reply_markup=reply_markup,
            **extra,
        ) for part in parts]
        return results[0]

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup: Optional[dict] = None
    ) -> dict[str, Any]:
        """Edit a message's text. Pass reply_markup to set/replace its keyboard,
        or {"inline_keyboard": []} to remove it; omitted, the field isn't sent."""
        return self._call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            link_preview_options=NO_LINK_PREVIEW,
            reply_markup=reply_markup,
        )

    def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: dict
    ) -> dict[str, Any]:
        """Replace a message's keyboard and leave its text (and any 👀 prefix) alone."""
        return self._call(
            "editMessageReplyMarkup",
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )

    def send_reply(
        self, chat_id: int, reply_to_message_id: int, text: str
    ) -> dict[str, Any]:
        """Send a message in reply to another message. (Kept as its own call, not
        send_message(reply_to=…): its recorded parameters stay those of 1.0.0.)
        Past TELEGRAM_LIMIT it sends every part as a reply to the same message and
        returns the first part's result."""
        results = [self._call(
            "sendMessage",
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            text=part,
            parse_mode="HTML",
            link_preview_options=NO_LINK_PREVIEW,
        ) for part in self.split_html(text)]
        return results[0]

    @staticmethod
    def split_html(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
        """Telegram HTML cut into parts of at most `limit` UTF-16 code units.

        A part ends at the last newline before the limit, else the last space (the
        newline or space itself is dropped), else as late as it fits. A cut never
        falls inside an entity (`&amp;`), a tag (`<i>`) or a surrogate pair. A tag
        open at a cut is closed at the end of the part and reopened at the start of
        the next. Text that fits is returned whole, as one part."""
        def units(s: str) -> int:
            return len(s.encode("utf-16-le")) // 2

        if units(text) <= limit:
            return [text]
        atoms: list[str] = []
        i = 0
        while i < len(text):
            match = (_TAG.match(text, i) if text[i] == "<" else
                     _ENTITY.match(text, i) if text[i] == "&" else None)
            atoms.append(match.group(0) if match else text[i])
            i += len(atoms[-1])

        def tag_name(atom: str) -> str:
            return atom.strip("</>").split(None, 1)[0].lower() if atom.strip("</>") else ""

        def is_open(atom: str) -> bool:
            return len(atom) > 2 and atom[0] == "<" and atom[1] != "/" and not atom.endswith("/>")

        def closing(stack: list[str]) -> str:
            return "".join(f"</{tag_name(t)}>" for t in reversed(stack))

        parts: list[str] = []
        stack: list[str] = []                    # the open tags, as written
        start = 0
        while start < len(atoms):
            prefix = "".join(stack)
            used = units(prefix)
            pos_stack = list(stack)
            # (atom index the part ends before, the stack there, the index the next part starts at)
            fit = newline = space = None
            j = start
            while j < len(atoms):
                atom = atoms[j]
                if atom in ("\n", " ") and j > start:
                    cut = (j, list(pos_stack), j + 1)
                    if used + units(closing(pos_stack)) <= limit:
                        if atom == "\n":
                            newline = cut
                        else:
                            space = cut
                cost = units(atom)
                after = list(pos_stack)
                if atom.startswith("</"):
                    name = tag_name(atom)
                    for k in range(len(after) - 1, -1, -1):
                        if tag_name(after[k]) == name:
                            del after[k]
                            break
                elif is_open(atom):
                    after.append(atom)
                if used + cost + units(closing(after)) > limit:
                    break
                used += cost
                pos_stack = after
                j += 1
                if not is_open(atom):
                    fit = (j, list(pos_stack), j)
            if j == len(atoms):
                parts.append(prefix + "".join(atoms[start:]))
                break
            end, end_stack, nxt = newline or space or fit or (start + 1, list(stack), start + 1)
            parts.append(prefix + "".join(atoms[start:end]) + closing(end_stack))
            stack = end_stack
            start = nxt
        return parts

    def answer_callback_query(
        self, callback_query_id: str, text: str, alert: bool = False
    ) -> None:
        """Acknowledge a callback query (button tap)."""
        self._call(
            "answerCallbackQuery",
            callback_query_id=callback_query_id,
            text=text,
            show_alert=alert,
        )

    def get_updates(
        self, offset: int = 0, timeout: int = 50
    ) -> list[dict[str, Any]]:
        """Long poll for updates (messages, callback queries). Telegram receives
        the requested long-poll duration as its own "timeout" form field; the
        local socket read timeout is padded above it so a slow, near-the-limit
        response isn't mistaken for a network error."""
        result = self._call(
            "getUpdates",
            http_timeout=timeout + 10,
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "callback_query"],
        )
        return result if isinstance(result, list) else []

    def get_file(self, file_id: str) -> dict[str, Any]:
        """getFile: {file_id, file_size, file_path}."""
        return self._call("getFile", file_id=file_id)

    @staticmethod
    def _open(url: str, timeout: float):
        return _FILE_OPENER.open(url, timeout=timeout)

    def download_file(self, file_path: str, max_bytes: int) -> bytes:
        """The file bytes, at most max_bytes (read with the cap, never whole). The URL carries the bot token,
        so no error text ever includes it, and a redirect is never followed. Every failure, a broken-off body
        (IncompleteRead, an HTTPException, not an OSError) included, is a TelegramError."""
        url = f"https://api.telegram.org/file/bot{self.bot_token}/{urllib.parse.quote(file_path)}"
        try:
            with self._open(url, 20) as resp:
                data = resp.read(max_bytes + 1)
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
            raise TelegramError("file download failed") from None
        if len(data) > max_bytes:
            raise FileTooLarge("file too large")
        return data

    @staticmethod
    def render_alert(project: str, title: str, instructable: Optional[dict[str, Any]] = None) -> str:
        """Only the ask headline and urgency; the full ask stays in Hook."""
        urgency = (instructable or {}).get("urgency", "today")
        urgency = urgency if urgency in ("now", "today", "later") else "today"
        return (f"🙋 <b>{TelegramClient._escape_clip(project, 300)}</b> — "
                f"{TelegramClient._escape_clip(title, 2000)}\nUrgency: {urgency}\nOpen in Hook")

    @staticmethod
    def alert_keyboard(item_id: str, open_base: Optional[str]) -> dict[str, Any]:
        rows = [[{"text": "📋 Open in Hook", "url": f"{open_base}/{item_id}"}]] if open_base else []
        return {"inline_keyboard": rows}

    @staticmethod
    def render_ask_message(
        project: str,
        ask: str,
        why: Optional[str],
        cmd: Optional[str],
        link: Optional[str],
        instructable: Optional[dict[str, Any]] = None,
    ) -> str:
        """Render an ask message in HTML (with parts only when provided).
        With an instructable the message is its summary instead (see
        _render_instructable); without one it is exactly as before."""
        if instructable:
            return TelegramClient._render_instructable(project, ask, why, instructable)
        lines = [f"🙋 <b>{html.escape(project)}</b> — {html.escape(ask)}"]
        if why:
            lines.append(f"<i>why:</i> {html.escape(why)}")
        if cmd:
            lines.append(f"<pre>{html.escape(cmd)}</pre>")
        if link:
            lines.append(f'<a href="{html.escape(link)}">link</a>')
        return "\n".join(lines)

    @staticmethod
    def _escape_clip(text: str, budget: int) -> str:
        """html.escape(text), cut on plain text so the result is at most
        `budget` characters: an entity is never split; "…" marks a cut."""
        escaped = html.escape(text)
        if len(escaped) <= budget:
            return escaped
        out, size = [], 0
        for ch in text:
            piece = html.escape(ch)
            if size + len(piece) > budget - 1:
                break
            out.append(piece)
            size += len(piece)
        return "".join(out) + "…"

    @staticmethod
    def _one_line(text: Any) -> str:
        """Multi-line text joined into one line with " / " (blank lines dropped)."""
        if not isinstance(text, str):
            return ""
        return " / ".join(part.strip() for part in text.splitlines() if part.strip())

    @staticmethod
    def _step_block(n: int, step: dict) -> list[str]:
        """One step's lines: "3. <do>", its one-line detail in italics, its
        "   📄 <file name> (on the Mac)" for its `file`, its "   ⚠️ <warning>",
        " (optional)" or " (required)" ending a line whose step is optional or has
        its own `required`, its "   ❓ <prompt>",
        then "   3a. <do>" per sub-step, each followed by its own "      📄 …",
        "      ⚠️ …" and "      ❓ <prompt>". Sub-step
        detail, code and url are never sent: the message is a summary and code
        stays in Hook."""
        block = [f"{n}. {html.escape(step.get('do') or '')}{TelegramClient._optional_mark(step)}"
                 f"{TelegramClient._required_mark(step)}"]
        warning = TelegramClient._warning_line(step, "   ")
        if warning:
            block.append(warning)
        detail = TelegramClient._one_line(step.get("detail"))
        if detail:
            block.append(f"{DETAIL_MARK}{TelegramClient._escape_clip(detail, MAX_RENDERED_DETAIL)}</i>")
        for extra in (TelegramClient._file_line(step.get("file"), "   "),
                      TelegramClient._question_line(step, "   ")):
            if extra:
                block.append(extra)
        subs = step.get("substeps")
        if isinstance(subs, list):
            for letter, sub in zip("abcdefghij", subs):
                text = sub.get("do") if isinstance(sub, dict) else None
                block.append(f"   {n}{letter}. {html.escape(text or '')}"
                             f"{TelegramClient._optional_mark(sub)}{TelegramClient._required_mark(sub)}")
                for extra in (TelegramClient._file_line(sub.get("file") if isinstance(sub, dict) else None,
                                                        "      "),
                              TelegramClient._warning_line(sub, "      "),
                              TelegramClient._question_line(sub, "      ")):
                    if extra:
                        block.append(extra)
        return block

    @staticmethod
    def _file_line(path: Any, indent: str) -> Optional[str]:
        """"<indent>📄 <file name> (on the Mac)" for a `file` or a link's `path`: only
        the last path component, since the file opens in Hook on the Mac, never here.
        None when there is no path."""
        if not isinstance(path, str) or not path.strip():
            return None
        name = path.rstrip("/").rsplit("/", 1)[-1] or path
        return f"{indent}📄 {TelegramClient._escape_clip(name, MAX_RENDERED_DETAIL)} (on the Mac)"

    @staticmethod
    def _optional_mark(step: Any) -> str:
        """" (optional)" for a step or sub-step with `optional: true`."""
        return " (optional)" if isinstance(step, dict) and step.get("optional") is True else ""

    @staticmethod
    def _required_mark(step: Any) -> str:
        """" (required)" for a step or sub-step with its own `required: true` (Pond
        1.3.0, RD 17): Telegram has no tint, so a text mark like " (optional)"."""
        return " (required)" if isinstance(step, dict) and step.get("required") is True else ""

    @staticmethod
    def _before_done_line(ins: Any) -> Optional[str]:
        """"Before Done: every step, a reply, a message from Hook" for the ask's `done`
        list, its parts always in that order; None without one (Pond 1.3.0, RD 17)."""
        done = ins.get("done") if isinstance(ins, dict) else None
        if not isinstance(done, list):
            return None
        parts = [words for key, words in BEFORE_DONE_PARTS if key in done]
        return "Before Done: " + ", ".join(parts) if parts else None

    @staticmethod
    def _warning_line(step: Any, indent: str) -> Optional[str]:
        """"<indent>⚠️ <warning>": the warning's first line, clipped like a detail;
        "Do this before the next steps" for a blocking step without one; None
        otherwise. Telegram can't lock steps, so this line is all it shows."""
        if not isinstance(step, dict):
            return None
        warning = step.get("warning")
        first = ""
        if isinstance(warning, str):
            first = next((part.strip() for part in warning.splitlines() if part.strip()), "")
        if not first and step.get("blocking") is True:
            first = BLOCKING_FALLBACK
        if not first:
            return None
        return f"{indent}⚠️ {TelegramClient._escape_clip(first, MAX_RENDERED_DETAIL)}"

    @staticmethod
    def _question_line(step: Any, indent: str) -> Optional[str]:
        """"<indent>❓ <prompt>": the prompt's first line, clipped like a detail;
        None when the step carries no question."""
        question = step.get("question") if isinstance(step, dict) else None
        prompt = question.get("prompt") if isinstance(question, dict) else None
        if not isinstance(prompt, str):
            return None
        first = next((part.strip() for part in prompt.splitlines() if part.strip()), "")
        return f"{indent}❓ {TelegramClient._escape_clip(first, MAX_RENDERED_DETAIL)}"

    @staticmethod
    def button_questions(instructable: Any, first_path: int = 1) -> tuple[list[tuple[str, dict]], int]:
        """→ ([(path, question)] answered by buttons, how many are left for Hook).
        Buttons are for single-pick questions (choices, not multi), the first
        MAX_QUESTION_ROWS of them in document order; multi-select, free-text and
        any past that count as left. Only top-level steps from `first_path` on count
        (Pond 1.9.0: a message that shows the steps an append added)."""
        asked = {path: q for path, q in _questions(instructable).items()
                 if int(path.partition(".")[0]) >= first_path}
        rows = [(path, q) for path, q in asked.items()
                if q.get("choices") and q.get("multi") is not True][:MAX_QUESTION_ROWS]
        return rows, len(asked) - len(rows)

    @staticmethod
    def _render_instructable(
        project: str, ask: str, why: Optional[str], ins: dict[str, Any]
    ) -> str:
        """Headline (⚡ first when urgency is "now"), why, up to 10 numbered
        `do` lines, each with its `detail` (one line, italic, clipped to
        MAX_RENDERED_DETAIL) under it, a "⚠️ <warning>" line for a step or
        sub-step with a warning or `blocking`, and its sub-steps as "   3a. <do>" lines,
        a "❓ <prompt>" line one level deeper under any step or sub-step with a
        question, then "+N more" for every step not shown, the "Before Done: …"
        line for a `done` list, a "📄 <file name> (on
        the Mac)" line per link with a `path` (while they fit; links with a url
        are never sent), and last "Answer N more in Hook" for the questions the
        keyboard has no buttons for. Code stays in Hook. The
        headline and why are clipped on plain text to a fixed budget. When a
        step's block doesn't fit, its detail line is dropped first; if it still
        doesn't fit, the step is left out whole (never shown with some sub-steps
        missing) and counted in "+N more". So the result stays under
        MAX_ASK_TEXT without ever cutting an HTML entity or tag."""
        clip = TelegramClient._escape_clip
        flag = "⚡ " if ins.get("urgency") == "now" else ""
        lines = [f"{flag}🙋 <b>{clip(project, 200)}</b> — {clip(ask, 800)}"]
        # One line only: a multi-line why could draw fake "✅ Done" or
        # "Open in Hook" lines under the headline.
        why = TelegramClient._one_line(ins.get("why") or why)
        if why:
            lines.append(f"<i>why:</i> {clip(why, 1000)}")
        warning = TelegramClient._warning_line(ins, "")
        if warning:
            lines.append(warning)
        steps = ins.get("steps") or []
        size = len("\n".join(lines))
        shown = 0
        _, left = TelegramClient.button_questions(ins)
        tail = f"Answer {left} more in Hook" if left else ""
        before_done = TelegramClient._before_done_line(ins)
        # Room kept for the lines that always come last: "Before Done: …" and the tail.
        kept = sum(len(line) + 1 for line in (before_done, tail) if line)

        def fits(block: list[str]) -> bool:
            # room for "\n+NN more", "\nBefore Done: …" and the "\nAnswer N more in Hook" line
            return size + len("\n".join(block)) + 1 <= MAX_ASK_TEXT - 16 - kept

        for n, step in enumerate(steps[:MAX_RENDERED_STEPS], 1):
            block = TelegramClient._step_block(n, step if isinstance(step, dict) else {})
            # The detail goes first when space runs out; a step is never shown with
            # some of its sub-steps missing, so after that it is left out whole.
            detail = next((i for i, line in enumerate(block) if line.startswith(DETAIL_MARK)), None)
            if not fits(block) and detail is not None:
                del block[detail]
            if not fits(block):
                break
            lines.extend(block)
            size += 1 + len("\n".join(block))
            shown += 1
        if len(steps) > shown:
            lines.append(f"+{len(steps) - shown} more")
            size += 1 + len(lines[-1])
        if before_done:
            lines.append(before_done)
            size += 1 + len(before_done)
        links = ins.get("links")
        for link in links if isinstance(links, list) else []:
            line = TelegramClient._file_line(link.get("path") if isinstance(link, dict) else None, "")
            if line is None:
                continue
            if size + 1 + len(line) > MAX_ASK_TEXT - (len(tail) + 1 if tail else 0):
                break
            lines.append(line)
            size += 1 + len(line)
        if tail:
            lines.append(tail)
        return "\n".join(lines)

    @staticmethod
    def render_ack_prefix() -> str:
        """Prefix for an acknowledged ask."""
        return "👀 "

    @staticmethod
    def render_resolved_message(ask: str, verdict: str, note: Optional[str]) -> str:
        """Render a resolved ask (struck through, with verdict and note)."""
        when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        emoji = "✅" if verdict == "done" else "🚫"
        verb = "done" if verdict == "done" else "skipped"
        first_line = html.escape(ask.split("\n")[0] if ask else ask)

        footer = f"<i>{emoji} {verb} {when}"
        if note:
            footer += f" — {html.escape(note)}"
        footer += "</i>"

        return f"✅ <s>{first_line}</s>\n{footer}"

    @staticmethod
    def render_corrected(project: str, title: str, rev: int, reason: str, summary: dict) -> str:
        """The message sent under an ask when its session corrected it: the version,
        the reason (escaped, one line), what changed by step path, and the call to
        review in Hook. Never any code: a changed command reads "command"."""
        clip = TelegramClient._escape_clip
        reason = TelegramClient._one_line(reason)

        def steps(entries: list, key: str = "path") -> str:
            return ", ".join(f"step {e[key]}" for e in entries) or "none"

        changed = ", ".join(
            f"step {c['path']} ({', '.join(FIELD_WORDS.get(f, f) for f in c['fields'])})"
            for c in summary.get("changed") or []) or "none"
        what = f"Changed: {changed}. Added: {steps(summary.get('added') or [])}. Removed: {steps(summary.get('removed') or [])}."
        if summary.get("moved"):
            what += " Moved: " + ", ".join(f"step {m['from']} → {m['to']}" for m in summary["moved"]) + "."
        if summary.get("top"):
            what += " Also: " + ", ".join(TOP_WORDS.get(k, k) for k in summary["top"]) + "."
        lines = [
            f"✏️ <b>Corrected</b> · version {rev}",
            f"<b>{clip(project, 200)}</b> · {clip(title, 300)}",
            f"The session says: <i>{clip(reason, 1300)}</i>",
            clip(what, 2000),
            "Review it in Hook before you act.",
        ]
        return "\n".join(lines)

    @staticmethod
    def render_appended(project: str, title: str, count: int, note: str, steps: list[tuple[int, dict]]) -> str:
        """The message sent under an ask when its session added steps (Pond 1.9.0):
        "➕ 2 steps added · <note>", the project and title, then each added step as
        (running number, step) through _step_block. Code stays in Hook; a block that
        doesn't fit loses its detail line first, then is left out and counted in
        "+N more", as in _render_instructable, so the text stays under MAX_ASK_TEXT."""
        clip = TelegramClient._escape_clip
        lines = [f"➕ {count} step{'' if count == 1 else 's'} added · {clip(TelegramClient._one_line(note), 400)}",
                 f"<b>{clip(project, 200)}</b> · {clip(title, 300)}"]
        size = len("\n".join(lines))
        shown = 0

        def fits(block: list[str]) -> bool:
            return size + len("\n".join(block)) + 1 <= MAX_ASK_TEXT - 16

        for n, step in steps[:MAX_RENDERED_STEPS]:
            block = TelegramClient._step_block(n, step if isinstance(step, dict) else {})
            detail = next((i for i, line in enumerate(block) if line.startswith(DETAIL_MARK)), None)
            if not fits(block) and detail is not None:
                del block[detail]
            if not fits(block):
                break
            lines.extend(block)
            size += 1 + len("\n".join(block))
            shown += 1
        if len(steps) > shown:
            lines.append(f"+{len(steps) - shown} more")
        return "\n".join(lines)

    @staticmethod
    def steps_added_line() -> str:
        """The line an ask's previous message gains when steps were added below it."""
        return "Steps added: see below"

    @staticmethod
    def render_superseded(title: str, rev: int) -> str:
        """What a corrected ask's previous message becomes."""
        return f"<s>{TelegramClient._escape_clip(title, 300)}</s>\nReplaced by version {rev} below."

    @staticmethod
    def render_session_note(text: str) -> str:
        """A session's /steps text: plain, escaped, under a fixed prefix, no links."""
        return "📝 Session note, not part of the ask:\n" + TelegramClient._escape_clip(text, 3900)

    @staticmethod
    def pending_keyboard(item_id: str, open_base: Optional[str]) -> dict[str, Any]:
        """The keyboard of an ask whose correction is not reviewed yet: Skip and
        Open in Hook only. Done and answers wait for the review in Hook."""
        rows: list[list[dict[str, str]]] = [[{"text": "🚫 Skip", "callback_data": f"s:{item_id}"}]]
        if open_base:
            rows.append([{"text": "📋 Open in Hook", "url": f"{open_base}/{item_id}"}])
        return {"inline_keyboard": rows}

    @staticmethod
    def ask_keyboard(
        item_id: str,
        open_base: Optional[str] = None,
        instructable: Optional[dict[str, Any]] = None,
        answers: Optional[dict[str, dict]] = None,
        *,
        rev: int = 1,
        ready: bool = True,
        first_path: int = 1,
    ) -> dict[str, Any]:
        """Inline keyboard for an open ask: one row per single-pick question
        (see button_questions), then Done / Skip, or Skip alone while the ask still
        needs something before the owner's Done (`ready` False, Pond 1.3.0). With open_base
        (MAILBOX_RELAY_OPEN_BASE) a last row opens the ask in Hook.

        A question row reads "3 · Staging", "Prod"; the current answer (`answers`,
        the counted answers by path) carries "✓ ". A button's callback_data is
        a:<item id>:<path>:<0-based choice index>[:<rev>] and Done's d:<item id>[:<rev>],
        bound to the version the message shows; Skip (s:<item id>) never is. The
        :<rev> suffix is only there from rev 2 on, so a rev-1 keyboard is byte-for-byte
        the 1.0.0 one and keeps working after a rollback (a missing rev reads as rev 1).
        A closed ask gets no keyboard at all (resolve clears it), so never question rows.
        With `first_path` (Pond 1.9.0) only questions of top-level steps from it on get rows."""
        suffix = f":{rev}" if rev > 1 else ""
        rows = TelegramClient._question_rows(item_id, instructable, answers, suffix, first_path)
        skip = {"text": "🚫 Skip", "callback_data": f"s:{item_id}"}
        rows.append([{"text": "✅ Done", "callback_data": f"d:{item_id}{suffix}"}, skip] if ready else [skip])
        if open_base:
            rows.append([{"text": "📋 Open in Hook", "url": f"{open_base}/{item_id}"}])
        return {"inline_keyboard": rows}

    @staticmethod
    def question_keyboard(item_id: str, instructable: Optional[dict[str, Any]], answers: Optional[dict[str, dict]],
                          *, rev: int, first_path: int = 1) -> dict[str, Any]:
        """The question rows of ask_keyboard only, no Done or Skip (Pond 1.9.0): what an
        ask's previous message keeps when steps were added below it."""
        suffix = f":{rev}" if rev > 1 else ""
        return {"inline_keyboard": TelegramClient._question_rows(item_id, instructable, answers, suffix, first_path)}

    @staticmethod
    def _question_rows(item_id: str, instructable: Optional[dict[str, Any]], answers: Optional[dict[str, dict]],
                       suffix: str, first_path: int) -> list[list[dict[str, str]]]:
        rows: list[list[dict[str, str]]] = []
        for path, question in TelegramClient.button_questions(instructable, first_path)[0]:
            picked = ((answers or {}).get(path) or {}).get("picked") or []
            row = []
            for index, choice in enumerate(choice_labels(question["choices"])):
                label = f"✓ {choice}" if choice in picked else choice
                data = f"a:{item_id}:{path}:{index}{suffix}"
                if len(data.encode("utf-8")) > MAX_CALLBACK_DATA:
                    raise ValueError("callback_data over Telegram's limit")
                row.append({"text": f"{path} · {label}" if index == 0 else label,
                            "callback_data": data})
            rows.append(row)
        return rows

    @staticmethod
    def render_notification(kind: str, title: str, body: Optional[str], open_asks: int = 0) -> str:
        """⏰ or ☀️, the title bold, the body lines, and the open-ask count on a brief."""
        icon = "⏰" if kind == "reminder" else "☀️"
        lines = [f"{icon} <b>{html.escape(title)}</b>"]
        if body:
            lines.append(html.escape(body))
        if open_asks:
            lines.append(f"Open asks: {open_asks}")
        return "\n".join(lines)

    @staticmethod
    def notification_keyboard(nid: str, actions: list[dict[str, str]]) -> dict[str, Any]:
        """One row of buttons; callback_data r:<notification id>:<action id>.
        Telegram caps callback_data at MAX_CALLBACK_DATA bytes: a uuid plus a
        verb fits, anything longer is refused here, not by Telegram at send time."""
        row = []
        for a in actions:
            data = f"r:{nid}:{a['id']}"
            if len(data.encode("utf-8")) > MAX_CALLBACK_DATA:
                raise ValueError("callback_data over Telegram's limit")
            row.append({"text": a["label"], "callback_data": data})
        return {"inline_keyboard": [row]}

    @staticmethod
    def render_notification_done(title: str, outcome: str) -> str:
        """The message after a reminder ended: struck through, with the outcome.
        `outcome` is done, tomorrow (the owner's tap), closed or gone (Central
        Time's verdict, recorded the same way)."""
        icon, note = NOTIFICATION_OUTCOMES[outcome]
        text = f"{icon} <s>{html.escape(title)}</s>"
        return f"{text}\n<i>{note}</i>" if note else text


# Telegram's callback_data limit, in bytes.
MAX_CALLBACK_DATA = 64
# A change summary's step fields as the owner reads them on Telegram.
FIELD_WORDS = {"code": "command", "url": "link", "file": "file", "question": "question", "doc": "document",
               "fields": "settings"}
# A change summary's top-level keys as the owner reads them; the rest keep their names.
TOP_WORDS = {"brief": "background"}
# outcome -> (icon, italic note or None) for render_notification_done.
NOTIFICATION_OUTCOMES = {
    "done": ("✅", None),
    "tomorrow": ("⏭", "moved to tomorrow"),
    "closed": ("✅", "already closed in Central Time"),
    "gone": ("🚫", "gone from Central Time"),
    "accept": ("🤝", "accepted"),
    "decline": ("↩️", "declined"),
    "stale": ("🚫", "changed in Central Time"),
}


class DisabledTelegram(TelegramClient):
    """A native Pond has no Telegram endpoint or delivery receipts."""

    enabled = False

    def __init__(self):
        self.owner_id = None

    def _call(self, method: str, **params) -> dict[str, Any]:
        return {}

    def download_file(self, file_path: str, max_bytes: int) -> bytes:
        raise TelegramError("Telegram is disabled")


class FakeTelegramClient(TelegramClient):
    """Test double for Telegram client (in-memory, no network)."""

    def __init__(self, bot_token: str = "test_token", owner_id: int = 12345):
        super().__init__(bot_token, owner_id)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sent_messages: list[dict[str, Any]] = []
        self.edited_messages: list[dict[str, Any]] = []
        self.edited_markups: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []
        self.callback_answers: list[dict[str, Any]] = []
        self.updates_to_return: list[list[dict[str, Any]]] = [[]]
        self.updates_call_count = 0
        # file id -> bytes served by download_file (getFile maps an id to photos/<id>.jpg);
        # file id -> the file_size getFile reports, when a test wants one.
        self.files: dict[str, bytes] = {}
        self.file_sizes: dict[str, int] = {}

    def download_file(self, file_path: str, max_bytes: int) -> bytes:
        self.calls.append(("download", {"file_path": file_path}))
        data = self.files.get(file_path.removeprefix("photos/").removesuffix(".jpg"))
        if data is None:
            raise TelegramError("file download failed")
        if len(data) > max_bytes:
            raise FileTooLarge("file too large")
        return data

    def _call(self, method: str, **params) -> dict[str, Any]:
        """Override to fake the API. Records every call verbatim in `calls`,
        plus typed, richer views (`sent_messages`, `edited_messages`, `replies`)
        that Tasks 3/4 assert on."""
        self.calls.append((method, dict(params)))
        if method == "sendMessage":
            msg = {
                "chat_id": params.get("chat_id"),
                "text": params.get("text"),
                "reply_markup": params.get("reply_markup"),
                "reply_to_message_id": params.get("reply_to_message_id"),
                "disable_notification": params.get("disable_notification", False),
            }
            self.sent_messages.append(msg)
            if msg["reply_to_message_id"] is not None:
                self.replies.append(msg)
            return {"message_id": len(self.sent_messages), "chat": {"id": params.get("chat_id")}}
        elif method == "editMessageText":
            msg = {
                "chat_id": params.get("chat_id"),
                "message_id": params.get("message_id"),
                "text": params.get("text"),
                "reply_markup": params.get("reply_markup"),
            }
            self.edited_messages.append(msg)
            return {"message_id": params.get("message_id")}
        elif method == "editMessageReplyMarkup":
            self.edited_markups.append({
                "chat_id": params.get("chat_id"),
                "message_id": params.get("message_id"),
                "reply_markup": params.get("reply_markup"),
            })
            return {"message_id": params.get("message_id")}
        elif method == "answerCallbackQuery":
            self.callback_answers.append(params)
            return {}
        elif method == "getFile":
            info = {"file_id": params.get("file_id"), "file_path": f"photos/{params.get('file_id')}.jpg"}
            if params.get("file_id") in self.file_sizes:
                info["file_size"] = self.file_sizes[params["file_id"]]
            return info
        elif method == "getUpdates":
            result = self.updates_to_return[self.updates_call_count] if self.updates_call_count < len(self.updates_to_return) else []
            self.updates_call_count += 1
            return result
        return {}
