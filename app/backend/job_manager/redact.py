"""Hides credentials in text that is stored and shown as a job's failure reason.

dirsearch (or Python) can echo a command line or a URL in an error. A reason is saved in the job and ends up in
the screen and in exported reports, so a password must not travel with it. Only secrets are hidden: hosts, paths,
ports and the rest of the message stay readable.
"""

from __future__ import annotations

import re

REDACTED = "<redacted>"

# scheme://user:password@host  ->  scheme://<redacted>@host
_URL_USERINFO = re.compile(r"(?P<scheme>\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s?#\"'<>@]+@")
# --proxy-auth user:pass, --auth=user:pass, --cookie "a=b", --data x ... (the option stays, its value goes)
_SECRET_OPTION = re.compile(
    r"(?P<option>--proxy-auth|--auth|--cookie|--data|--replay-proxy)(?P<sep>=|\s+)(?P<value>\"[^\"]*\"|'[^']*'|\S+)"
)
# -H "Authorization: Bearer x" / --header 'Cookie: a=b' (the header name stays)
_HEADER_OPTION = re.compile(
    r"(?P<option>-H|--header)(?P<sep>=|\s+)(?P<quote>[\"']?)(?P<name>[^:\"'\s]+):[^\"'\n]*(?P=quote)"
)
# Authorization: Bearer x, Proxy-Authorization: Basic x, Cookie: a=b (as a header line)
_SECRET_HEADER_LINE = re.compile(
    r"(?im)\b(?P<name>(?:proxy-)?authorization|cookie|set-cookie):[ \t]*[^\r\n\"']+"
)


def redact_secrets(text: str | None) -> str | None:
    if not text:
        return text
    text = _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", text)
    text = _SECRET_OPTION.sub(lambda m: f"{m.group('option')}{m.group('sep')}{REDACTED}", text)
    text = _HEADER_OPTION.sub(
        lambda m: f"{m.group('option')}{m.group('sep')}{m.group('quote')}{m.group('name')}: {REDACTED}{m.group('quote')}",
        text,
    )
    return _SECRET_HEADER_LINE.sub(lambda m: f"{m.group('name')}: {REDACTED}", text)
