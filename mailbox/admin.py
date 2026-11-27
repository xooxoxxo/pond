"""Pond admin commands, run inside the container (no token, no network):

    docker compose exec -T mailbox python3 -m mailbox.admin corrections on|off|status
    docker compose exec -T mailbox python3 -m mailbox.admin appends on|off|status
    docker compose exec -T mailbox python3 -m mailbox.admin docs on|off|status

`corrections` opens or closes POST /asks/{id}/amend (the `corrections_enabled`
setting). Off is the default: the Pond refuses every amend with 409 corrections
not enabled. Turning it off again keeps every revision already stored.

`appends` opens or closes POST /asks/{id}/append (Pond 1.9.0). On is the default;
off sets `appends_disabled` and the Pond refuses every append with 409 appends not
enabled. Corrections and appends are separate switches.

`docs` opens or closes documents on asks (Pond 1.10.0, the `docs_enabled` setting):
the `docs` request member and `doc` in an instructable. Off is the default: the Pond
refuses every post that has either (a `doc: null` too) with 409 documents not enabled,
and every amend or append that has a `docs` member or a `doc` the ask does not already
hold. An ask posted with documents while it was on can still be corrected and added to
while it keeps only those documents. A Hook bug that has either is refused too (a bug
never takes documents). Turning it off keeps every document already stored, and they
can still be read and released. Documents are kept until released, or until 7 days
after the ask is closed, whichever comes first; a reopened ask keeps its documents while
it is open. The expiry runs with the switch on or off. From Pond 1.12.0 the Pond keeps
at most 268,435,456 bytes (256 MiB) of document text in all, each stored text counted
once; a request whose new texts would go over it is refused with 409 document store
full, and releasing or expiring texts frees room. `docs status` also prints how many
are stored, their bytes in all with the limit, and the keep time.
"""
import os
import sys
import json
from pathlib import Path
from typing import Optional

from mailbox.admission import Admission, AdmissionError
from mailbox import docs as documents
from mailbox import maintenance
import sqlite3
from mailbox.docs import DOCS_KEEP_DAYS
from mailbox.service import SETTING_APPENDS_DISABLED, SETTING_CORRECTIONS, SETTING_DOCS
from mailbox.store import Store

USAGE = ("usage: python3 -m mailbox.admin corrections on|off|status\n"
         "       python3 -m mailbox.admin appends on|off|status\n"
         "       python3 -m mailbox.admin docs on|off|status")


def admission_command(args):
    replacing = len(args) == 4 and args[:3] == ["invite", "--recovery", "--replace"]
    if not replacing and args not in (["devices"], ["invite", "--first"], ["invite", "--recovery"]):
        print("usage: admission devices | invite --first | invite --recovery [--replace ID]", file=sys.stderr)
        return 2
    store = Store(Path(os.environ.get("MAILBOX_DATA_DIR", "/data")) / "mailbox.db")
    old_mask = os.umask(0o077)
    try:
        store.open()
        admission = Admission(store)
        if args == ["devices"]:
            print(json.dumps(admission.operator_devices()))
        else:
            grant = admission.operator_invite(args[1][2:], os.environ.get("MAILBOX_PUBLIC_ORIGIN"),
                                              replace_id=args[3] if replacing else None)
            print(grant["link"])
        return 0
    except AdmissionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        store.close()
        os.umask(old_mask)


def main(argv: Optional[list[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["status", "--json"]:
        try:
            print(json.dumps(maintenance.status(Path(os.environ.get("MAILBOX_DATA_DIR", "/data")))))
        except (OSError, ValueError, sqlite3.Error):
            print("Could not read managed Pond status", file=sys.stderr)
            return 1
        return 0
    if args[:1] == ["maintenance"]:
        try:
            result = maintenance.command(args[1:], Path(os.environ.get("MAILBOX_DATA_DIR", "/data")),
                                         Path(os.environ.get("MAILBOX_BACKUP_DIR", "/backups")))
        except (ValueError, OSError, sqlite3.Error, KeyError) as exc:
            print(f"Recovery command failed ({type(exc).__name__})", file=sys.stderr)
            return 1
        print(json.dumps(result))
        return 0
    if args[:1] == ["admission"]:
        return admission_command(args[1:])
    if len(args) != 2 or args[0] not in ("corrections", "appends", "docs") or args[1] not in ("on", "off", "status"):
        print(USAGE, file=sys.stderr)
        return 2
    store = Store(Path(os.environ.get("MAILBOX_DATA_DIR", "/data")) / "mailbox.db")
    store.open()
    totals = None
    try:
        if args[0] == "docs" and args[1] == "status":
            totals = store.doc_totals()
        if args[0] in ("corrections", "docs"):
            setting = SETTING_CORRECTIONS if args[0] == "corrections" else SETTING_DOCS
            if args[1] == "on":
                store.set_setting(setting, "1")
            elif args[1] == "off":
                store.delete_setting(setting)
            enabled = store.get_setting(setting) == "1"
        else:
            if args[1] == "on":
                store.delete_setting(SETTING_APPENDS_DISABLED)
            elif args[1] == "off":
                store.set_setting(SETTING_APPENDS_DISABLED, "1")
            enabled = store.get_setting(SETTING_APPENDS_DISABLED) != "1"
    finally:
        store.close()
    print(f"{args[0]}: {'on' if enabled else 'off'}")
    if totals is not None:
        print(f"docs stored: {totals[0]} ({totals[1]} of {documents.MAX_STORE_BYTES} bytes)")
        print(f"docs kept: until released, or {DOCS_KEEP_DAYS} days after the ask is closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
