#!/usr/bin/env python3
"""Bounded publisher-review inference, separate from ordinary self-hosted installs.

Run only on a private container network. The Hermes container receives no provider
key and has no direct internet route. Prompts and responses are never logged.
"""
import datetime
from contextlib import closing
import http.server
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import urllib.error
import urllib.request

MODEL = "deepseek-chat"
UPSTREAM = "https://api.deepseek.com/chat/completions"
MAX_BODY = 131072
MAX_TEXT = 16000
MAX_OUTPUT = 256
DAILY_REQUESTS = 50
MINUTE_REQUESTS = 5
MAX_RESPONSE = 2 * 1024 * 1024
STATE = Path(os.environ.get("REVIEW_PROXY_STATE", "/state"))
KEY_FILE = Path(os.environ.get("REVIEW_PROVIDER_KEY_FILE", "/run/secrets/provider-key"))
ACTIVE = threading.BoundedSemaphore(2)


class AdmissionError(ValueError):
    pass


def prepare(value):
    if not isinstance(value, dict) or value.get("model") != MODEL:
        raise AdmissionError("Only the review model is available.")
    messages = value.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 80:
        raise AdmissionError("Send between 1 and 80 messages.")
    output, total = [], 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant", "developer"}:
            raise AdmissionError("The review model supports text chat without tools.")
        text = message.get("content")
        if isinstance(text, list) and all(isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str) for p in text):
            text = "\n".join(p["text"] for p in text)
        if not isinstance(text, str):
            raise AdmissionError("The review model supports text. Use your own compatible provider for images.")
        total += len(text)
        if total > MAX_TEXT:
            raise AdmissionError("The review conversation is too long. Start a new chat.")
        output.append({"role": "system" if message["role"] == "developer" else message["role"], "content": text})
    requested = value.get("max_tokens", value.get("max_completion_tokens", MAX_OUTPUT))
    if type(requested) is not int or requested <= 0:
        raise AdmissionError("Invalid output limit.")
    if type(value.get("stream", False)) is not bool:
        raise AdmissionError("Invalid streaming option.")
    result = {"model": MODEL, "messages": output, "stream": value.get("stream", False), "max_tokens": min(requested, MAX_OUTPUT)}
    if value.get("response_format") == {"type": "json_object"}:
        result["response_format"] = {"type": "json_object"}
    return result


def admit(directory=STATE, now=None):
    now = time.time() if now is None else now
    day = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date().isoformat()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    database = directory / "budget.sqlite3"
    descriptor = os.open(database, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    database.chmod(0o600)
    with closing(sqlite3.connect(database, timeout=5, isolation_level=None)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS requests(at REAL NOT NULL, day TEXT NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        daily = db.execute("SELECT COUNT(*) FROM requests WHERE day=?", (day,)).fetchone()[0]
        minute = db.execute("SELECT COUNT(*) FROM requests WHERE at>?", (now - 60,)).fetchone()[0]
        if daily >= DAILY_REQUESTS or minute >= MINUTE_REQUESTS:
            raise AdmissionError("The shared review allowance is reached. Try later or connect your own Hermes server.")
        db.execute("INSERT INTO requests VALUES (?,?)", (now, day))
        db.execute("DELETE FROM requests WHERE at<?", (now - 172800,))
        db.commit()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "AgentReviewProvider"

    def log_message(self, *_args):
        pass

    def reply(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status, message):
        self.reply(status, {"error": {"message": message, "type": "review_provider_limit", "code": str(status)}})

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"ok": True})
        elif self.path in {"/models", "/v1/models"}:
            self.reply(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "review-provider"}]})
        else:
            self.fail(404, "Unknown endpoint.")

    def do_POST(self):
        if self.path not in {"/chat/completions", "/v1/chat/completions"}:
            self.fail(404, "Unknown endpoint.")
            return
        if self.headers.get("Transfer-Encoding"):
            self.fail(400, "Use a bounded request body.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise AdmissionError("Request body exceeds the review limit.")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise AdmissionError("Incomplete request body.")
            body = prepare(json.loads(raw))
        except (ValueError, UnicodeError):
            self.fail(400, "Invalid request or review limits exceeded. Use a short text conversation.")
            return
        if not ACTIVE.acquire(blocking=False):
            self.fail(429, "The shared review provider is busy. Try again shortly.")
            return
        started = False
        try:
            admit()
            key = KEY_FILE.read_text().strip()
            if not key or "\n" in key:
                raise OSError("Provider key unavailable")
            request = urllib.request.Request(UPSTREAM, data=json.dumps(body).encode(), method="POST",
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if body["stream"] else "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                started = True
                transferred = 0
                while chunk := response.read1(16384):
                    transferred += len(chunk)
                    if transferred > MAX_RESPONSE:
                        raise OSError("Provider response limit")
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except AdmissionError as exc:
            if not started:
                self.fail(429, str(exc))
        except urllib.error.HTTPError as exc:
            if not started:
                # Preserve non-retryable provider failures (notably an unfunded
                # account) without forwarding its body, headers, or credentials.
                status = exc.code if 400 <= exc.code < 500 else 503
                exc.close()
                self.fail(status, "The review provider is unavailable. Try later or connect your own Hermes server.")
        except (OSError, urllib.error.URLError, sqlite3.Error):
            if not started:
                self.fail(503, "The review provider is temporarily unavailable.")
        finally:
            ACTIVE.release()
            self.close_connection = True


if __name__ == "__main__":
    os.umask(0o077)
    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 16
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(65)
            return connection, address

    Server(("0.0.0.0", int(os.environ.get("REVIEW_PROXY_PORT", "18445"))), Handler).serve_forever()
