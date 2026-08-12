"""HTTP API and small ESPHome-like UI for ESP Anywhere Builder."""

from __future__ import annotations

from dataclasses import asdict
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any
from urllib.parse import unquote, urlparse

from builder_core import BuilderService, PROJECT_ID_PATTERN, new_project_source, short_error


MAX_JSON_BYTES = 512 * 1024
SESSION_TTL = 8 * 60 * 60


class ApiHandler(BaseHTTPRequestHandler):
    """Authenticated same-origin UI plus a bearer-compatible JSON API."""

    service: BuilderService
    token: str
    ui_path: Path

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok"}, authorize=False)
            return
        if path == "/":
            self._serve_ui()
            return
        if not self._authorized():
            return
        try:
            if path == "/v1/projects":
                self._json(HTTPStatus.OK, {"projects": [asdict(item) for item in self.service.store.list()]})
                return
            match = re.fullmatch(r"/v1/projects/([a-z0-9_-]{3,64})", path)
            if match:
                self._json(HTTPStatus.OK, asdict(self.service.store.get(match.group(1))))
                return
            match = re.fullmatch(r"/v1/projects/([a-z0-9_-]{3,64})/latest-artifacts", path)
            if match:
                self._json(HTTPStatus.OK, {"downloads": self.service.latest_downloads(match.group(1))})
                return
            match = re.fullmatch(r"/v1/projects/([a-z0-9_-]{3,64})/source", path)
            if match:
                self._json(HTTPStatus.OK, {"source": self.service.store.read_source(match.group(1))})
                return
            match = re.fullmatch(r"/v1/builds/([0-9a-f-]{36})", path)
            if match:
                self._json(HTTPStatus.OK, asdict(self.service.get_build(match.group(1))))
                return
            if path == "/v1/devices":
                self._json(HTTPStatus.OK, {"devices": self.service.devices()})
                return
            if path == "/v1/settings/advanced":
                self._json(HTTPStatus.OK, self.service.advanced_status(), no_store=True)
                return
            match = re.fullmatch(r"/v1/devices/([a-z0-9_-]{3,64})/logs", path)
            if match:
                self._json(HTTPStatus.OK, self.service.diagnostics(match.group(1)))
                return
            if path.startswith("/v1/artifacts/"):
                self._serve_artifact(path.removeprefix("/v1/artifacts/"))
                return
        except KeyError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        except (RuntimeError, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": short_error(str(error))})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/v1/projects":
                name = body.get("name", "")
                source = new_project_source(name) if body.get("create_new") is True else None
                item = self.service.store.create(name, body.get("yaml_file", ""), source)
                self._json(HTTPStatus.CREATED, asdict(item))
                return
            match = re.fullmatch(r"/v1/projects/([a-z0-9_-]{3,64})/validate", path)
            if match:
                self._json(HTTPStatus.OK, asdict(self.service.validate(match.group(1))))
                return
            match = re.fullmatch(r"/v1/projects/([a-z0-9_-]{3,64})/build", path)
            if match:
                item = self.service.create_build(match.group(1), body.get("mode", "build"), body.get("device_id"))
                self._json(HTTPStatus.ACCEPTED, asdict(item))
                return
            if path == "/v1/provision":
                item = self.service.provision(body.get("project_id", ""), body.get("build_id", ""), body.get("device_name", ""))
                self._json(HTTPStatus.CREATED, item, no_store=True)
                return
        except KeyError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        except RuntimeError as error:
            self._json(HTTPStatus.CONFLICT, {"error": short_error(str(error))})
            return
        except PermissionError:
            self._json(HTTPStatus.CONFLICT, {"error": "ESPHome project directory is not writable"})
            return
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": short_error(str(error))})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_PUT(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        path = urlparse(self.path).path
        try:
            body = self._body()
            match = re.fullmatch(r"/v1/projects/([a-z0-9_-]{3,64})/source", path)
            if match:
                item = self.service.store.write_source(match.group(1), body.get("source"))
                self._json(HTTPStatus.OK, asdict(item))
                return
            match = re.fullmatch(r"/v1/devices/([a-z0-9_-]{3,64})/project", path)
            if match:
                project_id = body.get("project_id", "")
                if PROJECT_ID_PATTERN.fullmatch(project_id) is None:
                    raise ValueError("Invalid project_id")
                self.service.store.assign_device(project_id, match.group(1))
                self._json(HTTPStatus.OK, {"device_id": match.group(1), "project_id": project_id})
                return
        except KeyError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": short_error(str(error))})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _serve_ui(self) -> None:
        session = self._new_session()
        body = self.ui_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", f"esp_anywhere_builder={session}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _serve_artifact(self, relative: str) -> None:
        decoded = unquote(relative)
        candidate = (self.service.artifact_dir / decoded).resolve()
        root = self.service.artifact_dir.resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file() or candidate.is_symlink():
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        immutable = "/builds/" in decoded and candidate.name != "firmware.manifest.json"
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if immutable else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not 1 <= length <= MAX_JSON_BYTES:
            raise ValueError("Invalid request size")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Request must be a JSON object")
        return value

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {self.token}":
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        value = cookie.get("esp_anywhere_builder")
        if value and self._valid_session(value.value):
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}, authorize=False)
        return False

    @classmethod
    def _new_session(cls) -> str:
        expires = str(int(time.time()) + SESSION_TTL)
        payload = f"{expires}.{secrets.token_urlsafe(18)}"
        signature = hmac.new(cls.token.encode(), payload.encode(), hashlib.sha256).digest()
        encoded = base64.urlsafe_b64encode(signature).decode().rstrip("=")
        return f"{payload}.{encoded}"

    @classmethod
    def _valid_session(cls, value: str) -> bool:
        try:
            expires, nonce, supplied = value.split(".", 2)
            if int(expires) <= int(time.time()) or not nonce:
                return False
            payload = f"{expires}.{nonce}"
            expected = base64.urlsafe_b64encode(
                hmac.new(cls.token.encode(), payload.encode(), hashlib.sha256).digest()
            ).decode().rstrip("=")
            return hmac.compare_digest(supplied, expected)
        except (TypeError, ValueError):
            return False

    def _json(self, status: HTTPStatus, payload: dict[str, Any], *, authorize: bool = True,
              no_store: bool = False) -> None:
        del authorize
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"builder-api {self.address_string()} {fmt % args}")


def main() -> None:
    token_file = Path(os.environ.get("BUILDER_TOKEN_FILE", "/run/secrets/builder_token"))
    token = token_file.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise SystemExit("Builder token must contain at least 32 characters")
    ApiHandler.service = BuilderService()
    ApiHandler.token = token
    ApiHandler.ui_path = Path(__file__).with_name("ui.html")
    port = int(os.environ.get("BUILDER_PORT", "8787"))
    server = ThreadingHTTPServer(("0.0.0.0", port), ApiHandler)
    print(f"ESP Anywhere Builder listening on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
