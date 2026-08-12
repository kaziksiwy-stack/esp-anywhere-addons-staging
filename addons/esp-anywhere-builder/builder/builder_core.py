"""Project, build and publishing primitives for the ESPHome-like Builder."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4

from poc_overlay import esphome_version, parse_target, run, sha256
from poc_worker_overlay import build as build_with_overlay
from poc_worker_overlay import report_entity_domains, validate_transport_target


PROJECT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
DEVICE_ID_PATTERN = PROJECT_ID_PATTERN
YAML_FILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. /-]{0,190}\.ya?ml$")
SEMVER_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
TERMINAL_BUILD_STAGES = frozenset({"ready", "failed", "rollback"})
BUILD_STAGES = frozenset({
    "validating", "compiling", "signing", "publishing", "installing",
    "health_check", *TERMINAL_BUILD_STAGES,
})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def project_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:56]
    if len(slug) < 3:
        slug = f"project-{slug or 'new'}"
    return slug


def next_patch_version(*versions: str | None) -> str:
    parsed: list[tuple[int, int, int]] = []
    for value in versions:
        if not value:
            continue
        match = SEMVER_PATTERN.fullmatch(value)
        if match:
            parsed.append(tuple(int(match.group(index)) for index in range(1, 4)))
    major, minor, patch = max(parsed, default=(0, 0, 0))
    return f"{major}.{minor}.{patch + 1}"


def new_project_source(name: str) -> str:
    friendly_name = json.dumps(name.strip(), ensure_ascii=False)
    return (
        f"esphome:\n  name: {project_slug(name)}\n  friendly_name: {friendly_name}\n\n"
        "esp32:\n  board: esp32-s3-devkitc-1\n  framework:\n    type: arduino\n\n"
        "logger:\n\nwifi:\n  ap: {}\n"
    )


def safe_relative_yaml(value: str) -> Path:
    if not isinstance(value, str) or YAML_FILE_PATTERN.fullmatch(value) is None:
        raise ValueError("Invalid ESPHome YAML path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("YAML path must stay inside the ESPHome config directory")
    return path


@dataclass(slots=True)
class ValidationRecord:
    status: str
    checked_at: str
    esphome_version: str
    target: dict[str, str]
    supported_domains: list[str]
    unsupported_entities: list[dict[str, Any]]
    error: str | None = None


@dataclass(slots=True)
class ProjectRecord:
    project_id: str
    name: str
    yaml_file: str
    created_at: str
    updated_at: str
    last_validate: dict[str, Any] | None = None
    last_build_id: str | None = None
    last_build_version: str | None = None
    esphome_version: str | None = None
    device_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BuildRecord:
    job_id: str
    build_id: str
    project_id: str
    mode: str
    stage: str = "validating"
    version: str | None = None
    device_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    error: str | None = None
    metadata: dict[str, Any] | None = None
    downloads: dict[str, str] = field(default_factory=dict)
    ota: dict[str, Any] | None = None


class ProjectStore:
    """Persist only project metadata; ESPHome YAML remains the source of truth."""

    def __init__(self, config_dir: Path, state_dir: Path) -> None:
        self.config_dir = config_dir.resolve()
        self.state_dir = state_dir
        self.state_file = state_dir / "projects.json"
        self._lock = threading.RLock()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._projects: dict[str, ProjectRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        document = json.loads(self.state_file.read_text(encoding="utf-8"))
        for raw in document.get("projects", []):
            item = ProjectRecord(**raw)
            self._projects[item.project_id] = item

    def _save(self) -> None:
        document = {"schema_version": 1, "projects": [asdict(item) for item in self._projects.values()]}
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_file)

    def source_path(self, yaml_file: str) -> Path:
        candidate = (self.config_dir / safe_relative_yaml(yaml_file)).resolve()
        if not candidate.is_relative_to(self.config_dir):
            raise ValueError("YAML path escapes the ESPHome config directory")
        return candidate

    def list(self) -> list[ProjectRecord]:
        with self._lock:
            return [ProjectRecord(**asdict(item)) for item in sorted(self._projects.values(), key=lambda value: value.name.lower())]

    def get(self, project_id: str) -> ProjectRecord:
        with self._lock:
            item = self._projects.get(project_id)
            if item is None:
                raise KeyError(project_id)
            return ProjectRecord(**asdict(item))

    def create(self, name: str, yaml_file: str, source: str | None = None) -> ProjectRecord:
        name = name.strip()
        if not name or len(name) > 80:
            raise ValueError("Project name must contain 1-80 characters")
        relative = safe_relative_yaml(yaml_file)
        project_id = project_slug(relative.stem)
        path = self.source_path(str(relative))
        with self._lock:
            if project_id in self._projects:
                raise ValueError("A project for this YAML already exists")
            if source is not None:
                if path.exists():
                    raise ValueError("ESPHome YAML already exists")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source, encoding="utf-8")
            if not path.is_file() or path.is_symlink():
                raise ValueError("ESPHome YAML does not exist or is a symlink")
            now = utc_now()
            item = ProjectRecord(project_id, name, str(relative), now, now)
            self._projects[project_id] = item
            self._save()
            return ProjectRecord(**asdict(item))

    def read_source(self, project_id: str) -> str:
        item = self.get(project_id)
        return self.source_path(item.yaml_file).read_text(encoding="utf-8")

    def write_source(self, project_id: str, source: str) -> ProjectRecord:
        if not isinstance(source, str) or not source.strip() or len(source.encode()) > 512 * 1024:
            raise ValueError("Invalid ESPHome YAML source")
        with self._lock:
            item = self._projects.get(project_id)
            if item is None:
                raise KeyError(project_id)
            path = self.source_path(item.yaml_file)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(source, encoding="utf-8")
            os.replace(temporary, path)
            item.updated_at = utc_now()
            item.last_validate = None
            self._save()
            return ProjectRecord(**asdict(item))

    def record_validation(self, project_id: str, validation: ValidationRecord) -> ProjectRecord:
        with self._lock:
            item = self._projects[project_id]
            item.last_validate = asdict(validation)
            item.esphome_version = validation.esphome_version
            item.updated_at = utc_now()
            self._save()
            return ProjectRecord(**asdict(item))

    def record_build(self, project_id: str, build: BuildRecord) -> None:
        with self._lock:
            item = self._projects[project_id]
            item.last_build_id = build.build_id
            item.last_build_version = build.version
            item.updated_at = utc_now()
            self._save()

    def assign_device(self, project_id: str, device_id: str) -> None:
        if DEVICE_ID_PATTERN.fullmatch(device_id) is None:
            raise ValueError("Invalid device_id")
        with self._lock:
            for item in self._projects.values():
                item.device_ids = [value for value in item.device_ids if value != device_id]
            item = self._projects.get(project_id)
            if item is None:
                raise KeyError(project_id)
            item.device_ids.append(device_id)
            item.device_ids.sort()
            item.updated_at = utc_now()
            self._save()

    def project_for_device(self, device_id: str) -> str | None:
        with self._lock:
            return next((item.project_id for item in self._projects.values() if device_id in item.device_ids), None)


class ImmutableArtifactPublisher:
    """Publish build directories once and update only a small channel pointer."""

    def __init__(self, artifact_dir: Path, public_base_url: str) -> None:
        self.artifact_dir = artifact_dir
        self.public_base_url = public_base_url.rstrip("/")
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

    def project_base_url(self, project_id: str) -> str:
        return f"{self.public_base_url}/projects/{quote(project_id)}"

    def firmware_url(self, project_id: str, build_id: str) -> str:
        return f"{self.project_base_url(project_id)}/builds/{quote(build_id)}/firmware.ota.bin"

    def publish(self, project_id: str, build_id: str, output: Path) -> Path:
        project_root = self.artifact_dir / "projects" / project_id
        target = project_root / "builds" / build_id
        if target.exists():
            raise ValueError("Immutable artifact build_id already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.publishing")
        if temporary.exists():
            shutil.rmtree(temporary)
        shutil.copytree(output, temporary)
        manifest = temporary / "firmware.ota-manifest.json"
        if not manifest.is_file():
            shutil.rmtree(temporary)
            raise ValueError("Build output does not contain the signed OTA manifest")
        os.replace(temporary, target)
        channel = project_root / "ota" / "stable" / "manifest.json"
        channel.parent.mkdir(parents=True, exist_ok=True)
        pointer = channel.with_suffix(".tmp")
        shutil.copy2(target / "firmware.ota-manifest.json", pointer)
        os.replace(pointer, channel)
        return target



class WorkerArtifactPublisher(ImmutableArtifactPublisher):
    """Publish immutable build artifacts to the staging Worker KV binding."""

    def __init__(self, artifact_dir: Path, public_base_url: str, endpoint: str,
                 installation_id: str, token_file: Path, bootstrap_pointer: bool = False) -> None:
        super().__init__(artifact_dir, public_base_url)
        self.endpoint = endpoint.rstrip("/")
        self.installation_id = installation_id
        self.token_file = token_file
        self.bootstrap_pointer = bootstrap_pointer

    def _upload(self, key: str, source: Path) -> None:
        token = self.token_file.read_text(encoding="utf-8").strip()
        content_type = "application/json" if source.suffix == ".json" else "application/octet-stream"
        request = Request(
            f"{self.endpoint}/builder/artifact?installation_id={quote(self.installation_id)}",
            method="PUT", data=source.read_bytes(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type,
                     "User-Agent": "ESP-Anywhere-Builder/1.0", "X-Artifact-Key": key},
        )
        try:
            with urlopen(request, timeout=60) as response:
                if response.status != 201:
                    raise RuntimeError(f"Artifact upload returned HTTP {response.status}")
        except HTTPError as error:
            if error.code == 409:
                raise ValueError("Immutable artifact already exists in Worker storage") from None
            raise RuntimeError(f"Artifact upload returned HTTP {error.code}") from None
        except URLError as error:
            raise RuntimeError("Artifact storage is unavailable") from error

    def publish(self, project_id: str, build_id: str, output: Path) -> Path:
        target = super().publish(project_id, build_id, output)
        prefix = f"projects/{project_id}/builds/{build_id}"
        for source in sorted(target.iterdir()):
            if source.is_file():
                self._upload(f"{prefix}/{source.name}", source)
        manifest = target / "firmware.ota-manifest.json"
        self._upload(f"projects/{project_id}/ota/stable/manifest.json", manifest)
        deadline = time.monotonic() + 90
        pointer_url = f"{self.project_base_url(project_id)}/ota/stable/manifest.json"
        while True:
            try:
                with urlopen(Request(pointer_url, headers={"User-Agent": "ESP-Anywhere-Builder/1.0"}), timeout=10) as response:
                    outer = json.loads(response.read())
                payload = json.loads(base64.b64decode(outer["signed_payload"], validate=True))
                if payload.get("build_id") == build_id:
                    break
            except (HTTPError, URLError, KeyError, ValueError, json.JSONDecodeError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Published artifact pointer did not become readable")
            time.sleep(2)
        if self.bootstrap_pointer:
            self._upload("ota/stable/manifest.json", manifest)
        return target

class GitArtifactPublisher(ImmutableArtifactPublisher):
    """Mirror immutable artifacts to a repository-scoped Git publication path."""

    def __init__(self, artifact_dir: Path, public_base_url: str, repository: str,
                 deploy_key: Path, known_hosts: Path, git_name: str, git_email: str,
                 bootstrap_pointer: bool = False) -> None:
        super().__init__(artifact_dir, public_base_url)
        self.repository = repository
        self.deploy_key = deploy_key
        self.known_hosts = known_hosts
        self.git_name = git_name
        self.git_email = git_email
        self.bootstrap_pointer = bootstrap_pointer

    def publish(self, project_id: str, build_id: str, output: Path) -> Path:
        target = super().publish(project_id, build_id, output)
        with tempfile.TemporaryDirectory(prefix="esp-anywhere-publish-") as raw:
            checkout = Path(raw) / "repo"
            env = os.environ.copy()
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {self.deploy_key} -o IdentitiesOnly=yes "
                f"-o UserKnownHostsFile={self.known_hosts} -o StrictHostKeyChecking=yes"
            )
            self._run(["git", "clone", f"git@github.com:{self.repository}.git", str(checkout)], env=env)
            relative = Path("projects") / project_id / "builds" / build_id
            if (checkout / relative).exists():
                raise ValueError("Immutable artifact already exists in publication repository")
            (checkout / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(target, checkout / relative)
            channel = checkout / "projects" / project_id / "ota" / "stable" / "manifest.json"
            channel.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target / "firmware.ota-manifest.json", channel)
            if self.bootstrap_pointer:
                bootstrap = checkout / "ota" / "stable" / "manifest.json"
                bootstrap.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target / "firmware.ota-manifest.json", bootstrap)
            self._run(["git", "config", "user.name", self.git_name], cwd=checkout)
            self._run(["git", "config", "user.email", self.git_email], cwd=checkout)
            paths = [str(relative), str(channel.relative_to(checkout))]
            if self.bootstrap_pointer:
                paths.append("ota/stable/manifest.json")
            self._run(["git", "add", *paths], cwd=checkout)
            self._run(["git", "commit", "-m", f"Publish {project_id} build {build_id}"], cwd=checkout)
            self._run(["git", "push", "origin", "main"], cwd=checkout, env=env)
        return target

    @staticmethod
    def _run(args: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
        result = subprocess.run(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=300, check=False)
        if result.returncode:
            raise RuntimeError(result.stdout)
        return result.stdout


class WorkerClient:
    """Small HA-authorized HTTP client; device wire protocol remains unchanged."""

    def __init__(self, endpoint: str, installation_id: str, token_file: Path) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.installation_id = installation_id
        self.token_file = token_file

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.installation_id and self.token_file.is_file())

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if not self.configured:
            raise RuntimeError("Worker management connection is not configured")
        token = self.token_file.read_text(encoding="utf-8").strip()
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        request = Request(
            f"{self.endpoint}{path}", method=method, data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "User-Agent": "ESP-Anywhere-Builder/1.0"},
        )
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read())
        except HTTPError as error:
            detail = error.read().decode(errors="replace")[:200]
            raise RuntimeError(f"Worker returned HTTP {error.code}: {detail}") from None
        except URLError as error:
            raise RuntimeError("Worker is unavailable") from error

    def list_devices(self) -> list[dict[str, Any]]:
        result = self._request("GET", f"/ha/devices?installation_id={quote(self.installation_id)}")
        return result.get("devices", [])

    def create_activation(self, device_id: str) -> dict[str, Any]:
        return self._request("POST", "/ha/device-activation-code", {
            "installation_id": self.installation_id, "device_id": device_id,
        })

    def start_ota(self, device_id: str, command_id: str, version: str) -> dict[str, Any]:
        return self._request("POST", "/ha/ota-start", {
            "installation_id": self.installation_id, "device_id": device_id,
            "command_id": command_id, "channel": "stable", "target_version": version,
            "recovery": False,
        })

    def ota_status(self, device_id: str, command_id: str) -> dict[str, Any]:
        path = (f"/ha/ota-status?installation_id={quote(self.installation_id)}"
                f"&device_id={quote(device_id)}&command_id={quote(command_id)}")
        return self._request("GET", path)


class BuilderService:
    """ESPHome-first Builder with asynchronous builds and optional remote install."""

    def __init__(self, *, build_runner: Callable[..., dict[str, Any]] = build_with_overlay) -> None:
        self.config_dir = Path(os.environ.get("CONFIG_DIR", "/config"))
        self.work_dir = Path(os.environ.get("WORK_DIR", "/work"))
        self.state_dir = Path(os.environ.get("STATE_DIR", str(self.work_dir / "state")))
        self.artifact_dir = Path(os.environ.get("ARTIFACT_DIR", str(self.work_dir / "artifacts")))
        self.signing_key = Path(os.environ.get("SIGNING_KEY", "/run/secrets/firmware_signing_key"))
        self.key_id = os.environ.get("SIGNING_KEY_ID", "firmware-prod-2026-01")
        self.esphome = os.environ.get("ESPHOME", "esphome")
        self.public_base_url = os.environ.get("PUBLIC_ARTIFACT_BASE_URL", "http://127.0.0.1:8787/v1/artifacts")
        self.store = ProjectStore(self.config_dir, self.state_dir)
        installation_id = os.environ.get("INSTALLATION_ID", "")
        installation_file = Path(os.environ.get("INSTALLATION_ID_FILE", "/run/secrets/installation_id"))
        if not installation_id and installation_file.is_file():
            installation_id = installation_file.read_text(encoding="utf-8").strip()
        worker_endpoint = os.environ.get("WORKER_URL", "")
        worker_token_file = Path(os.environ.get("WORKER_HA_TOKEN_FILE", "/run/secrets/worker_ha_token"))
        repository = os.environ.get("OTA_REPOSITORY", "").strip()
        deploy_key = Path(os.environ.get("DEPLOY_KEY", "/run/secrets/github_deploy_key"))
        known_hosts = Path(os.environ.get("KNOWN_HOSTS", "/run/secrets/github_known_hosts"))
        if os.environ.get("ARTIFACT_PUBLISHER") == "worker":
            if not worker_endpoint or not installation_id or not worker_token_file.is_file():
                raise RuntimeError("Worker artifact publisher is not configured")
            self.publisher = WorkerArtifactPublisher(
                self.artifact_dir, self.public_base_url, worker_endpoint, installation_id, worker_token_file,
                os.environ.get("PUBLISH_BOOTSTRAP_POINTER", "false").lower() == "true",
            )
        elif repository and deploy_key.is_file() and known_hosts.is_file():
            self.publisher: ImmutableArtifactPublisher = GitArtifactPublisher(
                self.artifact_dir, self.public_base_url, repository, deploy_key, known_hosts,
                os.environ.get("GIT_AUTHOR_NAME", "ESP Anywhere Builder"),
                os.environ.get("GIT_AUTHOR_EMAIL", "esp-anywhere-builder@localhost"),
                os.environ.get("PUBLISH_BOOTSTRAP_POINTER", "false").lower() == "true",
            )
        else:
            self.publisher = ImmutableArtifactPublisher(self.artifact_dir, self.public_base_url)
        self.worker = WorkerClient(worker_endpoint, installation_id, worker_token_file)
        self._build_runner = build_runner
        self._lock = threading.RLock()
        self._active_job: str | None = None
        self.jobs: dict[str, BuildRecord] = {}
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = Path(os.environ.get("BUILDER_TMP_DIR", str(self.work_dir / "tmp")))
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def validate(self, project_id: str) -> ValidationRecord:
        project = self.store.get(project_id)
        source = self.store.source_path(project.yaml_file)
        version = esphome_version(self.esphome, source.parent)
        try:
            normalized = run([self.esphome, "config", str(source)], source.parent)
            target = parse_target(normalized)
            validate_transport_target(target)
            supported, unsupported = report_entity_domains(normalized)
            record = ValidationRecord("valid", utc_now(), version, target, supported, unsupported)
        except Exception as error:
            record = ValidationRecord("invalid", utc_now(), version, {}, [], [], short_error(str(error)))
        self.store.record_validation(project_id, record)
        return record

    def create_build(self, project_id: str, mode: str, device_id: str | None = None) -> BuildRecord:
        if mode not in {"build", "manual", "usb", "wireless"}:
            raise ValueError("Invalid install mode")
        if mode == "wireless" and (not device_id or DEVICE_ID_PATTERN.fullmatch(device_id) is None):
            raise ValueError("Wireless install requires a paired device")
        project = self.store.get(project_id)
        source_digest = sha256(self.store.source_path(project.yaml_file))[:12]
        build_id = (
            f"{project.project_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
            f"-{source_digest}-{uuid4().hex[:8]}"
        )
        job = BuildRecord(str(uuid4()), build_id, project_id, mode, device_id=device_id)
        with self._lock:
            if self._active_job is not None:
                raise RuntimeError("Another build is already running")
            self._active_job = job.job_id
            self.jobs[job.job_id] = job
        threading.Thread(target=self._run_build, args=(job,), daemon=True).start()
        return self.get_build(job.job_id)

    def get_build(self, job_id: str) -> BuildRecord:
        with self._lock:
            item = self.jobs.get(job_id)
            if item is None:
                raise KeyError(job_id)
            return BuildRecord(**asdict(item))

    def _stage(self, job: BuildRecord, stage: str) -> None:
        if stage not in BUILD_STAGES:
            raise ValueError("Invalid build stage")
        with self._lock:
            job.stage = stage

    def _run_build(self, job: BuildRecord) -> None:
        temporary = self.work_dir / "jobs" / job.job_id
        try:
            validation = self.validate(job.project_id)
            if validation.status != "valid":
                raise ValueError(validation.error or "ESPHome configuration is invalid")
            project = self.store.get(job.project_id)
            current_device_version = None
            if job.device_id and self.worker.configured:
                current_device_version = next((item.get("firmware_version") for item in self.worker.list_devices()
                                               if item.get("device_id") == job.device_id), None)
            job.version = next_patch_version(project.last_build_version, current_device_version)
            self._stage(job, "compiling")
            output = temporary / "output"
            values = {
                "artifact_name": "firmware",
                "friendly_name": project.name,
                "model": f"ESPHome project {project.name}",
                "firmware_version": job.version,
                "build_id": job.build_id,
                "ota_private_key": str(self.signing_key),
                "ota_key_id": self.key_id,
                "ota_firmware_url": self.publisher.firmware_url(job.project_id, job.build_id),
                "ota_base_url": self.publisher.project_base_url(job.project_id),
                "ota_channel": "stable",
                "ota_recovery": False,
            }
            metadata = self._build_runner(
                self.store.source_path(project.yaml_file), output, values, self.esphome,
            )
            self._stage(job, "signing")
            metadata["project_id"] = project.project_id
            metadata["build_id"] = job.build_id
            metadata["version"] = job.version
            metadata["created_at"] = job.created_at
            (output / "build.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
            self._stage(job, "publishing")
            published = self.publisher.publish(job.project_id, job.build_id, output)
            # Relative links also work below a Home Assistant Ingress prefix.
            base = f"v1/artifacts/projects/{job.project_id}/builds/{job.build_id}"
            job.downloads = {
                "factory": f"{base}/firmware.factory.bin",
                "ota": f"{base}/firmware.ota.bin",
                "metadata": f"{base}/build.json",
                "manifest": f"{base}/firmware.ota-manifest.json",
                "web_manifest": f"{base}/firmware.manifest.json",
            }
            job.metadata = metadata
            self.store.record_build(job.project_id, job)
            if job.device_id:
                self.store.assign_device(job.project_id, job.device_id)
            if job.mode == "wireless":
                self._run_wireless_install(job)
            else:
                self._stage(job, "ready")
            job.finished_at = utc_now()
            del published
        except Exception as error:  # job boundary
            job.error = short_error(str(error))
            job.finished_at = utc_now()
            self._stage(job, "failed")
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
            with self._lock:
                self._active_job = None

    def _run_wireless_install(self, job: BuildRecord) -> None:
        assert job.device_id and job.version
        self._stage(job, "installing")
        command_id = str(uuid4())
        self.worker.start_ota(job.device_id, command_id, job.version)
        deadline = time.monotonic() + 360
        next_delivery_retry = time.monotonic() + 10
        delivery_retries = 0
        while time.monotonic() < deadline:
            status = self.worker.ota_status(job.device_id, command_id)
            job.ota = status
            state = status.get("state")
            if state == "queued" and time.monotonic() >= next_delivery_retry and delivery_retries < 3:
                # A Worker deployment can leave a hibernated socket looking online
                # briefly. Re-send the same idempotent command after reconnect instead
                # of asking the user to start a second build.
                self.worker.start_ota(job.device_id, command_id, job.version)
                delivery_retries += 1
                next_delivery_retry = time.monotonic() + 10
            if state in {"rebooting", "confirmed"}:
                self._stage(job, "health_check")
            if state == "confirmed":
                self._stage(job, "ready")
                return
            if state == "rollback":
                self._stage(job, "rollback")
                job.error = status.get("error_code") or "Firmware rolled back"
                return
            if state == "failed":
                raise RuntimeError(status.get("error_code") or "OTA failed")
            time.sleep(2)
        raise TimeoutError("Device did not complete OTA health-check")

    def devices(self) -> list[dict[str, Any]]:
        if not self.worker.configured:
            return []
        result = []
        for raw in self.worker.list_devices():
            result.append({
                "friendly_name": raw.get("friendly_name") or raw.get("device_id"),
                "device_id": raw.get("device_id"),
                "online": raw.get("online") is True,
                "firmware_version": raw.get("firmware_version"),
                "project_id": self.store.project_for_device(raw.get("device_id", "")),
                "chip_family": raw.get("chip_family"),
                "ota_capability": raw.get("ota_capability"),
                "last_seen": raw.get("last_seen"),
                "ota": raw.get("ota"),
            })
        return result

    def provision(self, project_id: str, build_id: str, device_name: str) -> dict[str, Any]:
        project = self.store.get(project_id)
        if project.last_build_id != build_id:
            raise ValueError("Select the latest completed project build")
        device_id = f"{project_slug(device_name)[:48]}-{uuid4().hex[:6]}"
        activation = self.worker.create_activation(device_id)
        self.store.assign_device(project_id, device_id)
        return {
            "device_name": device_name[:64], "device_id": device_id,
            "relay_url": self.worker.endpoint, "installation_id": self.worker.installation_id,
            "activation_code": activation["code"], "expires_at": activation.get("expiresAt"),
        }

    def diagnostics(self, device_id: str) -> dict[str, Any]:
        item = next((value for value in self.devices() if value["device_id"] == device_id), None)
        if item is None:
            raise KeyError(device_id)
        return {
            "device_id": device_id, "online": item["online"], "firmware_version": item["firmware_version"],
            "last_seen": item["last_seen"], "ota": item["ota"],
            "message": "Full live ESPHome logs are not available yet; these are ESP Anywhere diagnostics.",
        }


    def advanced_status(self) -> dict[str, Any]:
        """Expose infrastructure readiness without ever returning credentials."""
        return {
            "firmware_signing": "ready" if self.signing_key.is_file() else "not_ready",
            "key_id": self.key_id if self.signing_key.is_file() else None,
            "worker_endpoint": self.worker.endpoint or None,
            "artifact_endpoint": self.public_base_url or None,
            "installation_id": self.worker.installation_id or None,
            "worker_credentials": "ready" if self.worker.configured else "not_ready",
        }


def short_error(value: str) -> str:
    clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", value)
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    diagnostic = [line for line in lines if re.search(r"(?i)(fatal error:|error:|\[error\]|exception:)", line)]
    message = diagnostic[0] if diagnostic else (lines[-1] if lines else "Operation failed")
    message = re.sub(r"(?i)(password|token|secret)(\s*[:=]\s*)\S+", r"\1\2<redacted>", message)
    return message[:500]
