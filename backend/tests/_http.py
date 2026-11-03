"""Browser test clients submit the same CSRF header as the UI."""
import re


def enable_csrf(client):
    match = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/compare").text)
    assert match, "Authenticated page must expose a CSRF token"
    client.headers["X-CSRF-Token"] = match.group(1)
