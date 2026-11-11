from backend.job_manager.redact import redact_secrets


def test_credentials_are_hidden_and_everything_else_stays_readable() -> None:
    text = (
        "ConnectError via http://admin:s3cret@127.0.0.1:8080/ to https://target.local:8443/api/v1\n"
        "dirsearch --proxy-auth bob:pw12345 --auth=ann:hunter2 --cookie 'sid=abc' -H \"Authorization: Bearer tok123\" "
        "--header 'X-Api: 1' -u https://target.local/\n"
        "Proxy-Authorization: Basic Zm9vOmJhcg==\nCookie: session=zzz\n"
        "Read timed out on /admin?q=word status 403"
    )
    out = redact_secrets(text)
    for secret in ("s3cret", "pw12345", "hunter2", "sid=abc", "tok123", "Zm9vOmJhcg", "session=zzz"):
        assert secret not in out, secret
    # what a person needs to understand the failure is still there
    for kept in ("http://<redacted>@127.0.0.1:8080/", "https://target.local:8443/api/v1", "--proxy-auth", "-u https://target.local/",
                 "Read timed out on /admin?q=word status 403", "Authorization: <redacted>", "X-Api: <redacted>"):
        assert kept in out, kept


def test_nothing_to_hide_returns_the_text_unchanged() -> None:
    for text in ("dirsearch exited with code 2", "Unreachable host", "usage: -c [-u|--url] URL\n-c: error: unknown option", None, ""):
        assert redact_secrets(text) == text
