"""Static regression checks for the supported Docker Compose topologies.

These tests intentionally use ``docker compose config`` instead of starting
containers.  They are safe on CPU-only CI hosts and catch topology, port, GPU
reservation, and basic hardening regressions before model images are pulled.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
STREAMING_VAD_OVERRIDE = PROJECT_ROOT / "docker-compose.streaming-vad.yml"

CORE_SERVICES = {"mysql", "master-key-init", "backend", "tag-worker", "frontend"}
PROFILE_SERVICES = {
    "mock": CORE_SERVICES | {"adminer"},
    "cache-redis": CORE_SERVICES | {"redis"},
    # bge-m3-cache-init warms tei_cache for whichever TEI variant the profile
    # selects, so it belongs to all three. It is deliberately absent from
    # MODEL_SERVICES below: a one-shot container that exits 0 is depended on
    # via service_completed_successfully and has no healthcheck to assert.
    "models-cpu": CORE_SERVICES | {"bge-m3-cache-init", "bge-m3-cpu", "campplus-service", "funasr"},
    "models-single-gpu": CORE_SERVICES
    | {
        "bge-m3-cache-init",
        "bge-m3-gpu",
        "campplus-service",
        "clap-service",
        "funasr",
        "vllm-strong",
    },
    "models-multi-gpu": CORE_SERVICES
    | {
        "bge-m3-cache-init",
        "bge-m3-gpu",
        "campplus-service",
        "clap-service",
        "funasr",
        "vllm-strong",
        "vllm-weak",
    },
}
GPU_SERVICES = {"bge-m3-gpu", "clap-service", "vllm-strong", "vllm-weak"}
MODEL_SERVICES = {
    "bge-m3-cpu",
    "bge-m3-gpu",
    "campplus-service",
    "clap-service",
    "funasr",
    "vllm-strong",
    "vllm-weak",
}
CUSTOM_MODEL_SERVICES = {"campplus-service", "clap-service"}


def _require_compose() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI is not installed")
    probe = subprocess.run(
        ["docker", "compose", "version"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("docker compose plugin is not installed")


@cache
def _compose(profile: str) -> dict[str, Any]:
    _require_compose()
    environment = os.environ.copy()
    environment.pop("COMPOSE_APP_BIND_HOST", None)
    environment.pop("COMPOSE_PRIVATE_BIND_HOST", None)
    environment.pop("REDIS_URL", None)
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "--file",
            str(COMPOSE_FILE),
            "--profile",
            profile,
            "config",
            "--format",
            "json",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(("profile", "expected"), PROFILE_SERVICES.items())
def test_profile_resolves_to_expected_services(profile: str, expected: set[str]) -> None:
    assert set(_compose(profile)["services"]) == expected


def test_redis_is_not_configured_for_apps_unless_explicitly_requested() -> None:
    services = _compose("mock")["services"]
    assert services["backend"]["environment"]["REDIS_URL"] == ""
    assert services["tag-worker"]["environment"]["REDIS_URL"] == ""


@pytest.mark.parametrize("profile", PROFILE_SERVICES)
def test_profile_has_no_host_port_collisions(profile: str) -> None:
    published_by_port: dict[tuple[str, str], str] = {}
    for service_name, service in _compose(profile)["services"].items():
        for port in service.get("ports", []):
            key = (str(port.get("host_ip", "0.0.0.0")), str(port["published"]))
            previous = published_by_port.setdefault(key, service_name)
            assert previous == service_name, (
                f"{profile}: host port {key} is published by both {previous} and {service_name}"
            )


@pytest.mark.parametrize("profile", PROFILE_SERVICES)
def test_all_published_ports_default_to_loopback(profile: str) -> None:
    for service_name, service in _compose(profile)["services"].items():
        for port in service.get("ports", []):
            assert port.get("host_ip") == "127.0.0.1", (
                f"{profile}: {service_name} publishes {port['published']} "
                "outside the loopback interface"
            )


@pytest.mark.parametrize("profile", ("mock", "models-multi-gpu"))
def test_app_bind_override_does_not_expose_private_services(profile: str) -> None:
    _require_compose()
    environment = os.environ.copy()
    environment["COMPOSE_APP_BIND_HOST"] = "0.0.0.0"
    environment["COMPOSE_PRIVATE_BIND_HOST"] = "127.0.0.1"
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "--file",
            str(COMPOSE_FILE),
            "--profile",
            profile,
            "config",
            "--format",
            "json",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    for service_name, service in services.items():
        expected_host = "0.0.0.0" if service_name in {"backend", "frontend"} else "127.0.0.1"
        assert {port.get("host_ip") for port in service.get("ports", [])} <= {expected_host}


@pytest.mark.parametrize("profile", PROFILE_SERVICES)
def test_all_images_are_version_pinned(profile: str) -> None:
    for service_name, service in _compose(profile)["services"].items():
        image = service.get("image")
        if image is None:
            continue
        assert not image.endswith(":latest"), f"{service_name} uses a mutable latest tag"
        assert "@" in image or ":" in image.rsplit("/", 1)[-1], (
            f"{service_name} image is not version-pinned: {image}"
        )


@pytest.mark.parametrize("profile", ("models-single-gpu", "models-multi-gpu"))
def test_gpu_services_reserve_one_explicit_device(profile: str) -> None:
    services = _compose(profile)["services"]
    for service_name in GPU_SERVICES & services.keys():
        devices = (
            services[service_name]
            .get("deploy", {})
            .get("resources", {})
            .get("reservations", {})
            .get("devices", [])
        )
        assert len(devices) == 1, f"{service_name} must reserve exactly one GPU"
        reservation = devices[0]
        assert reservation.get("device_ids") and len(reservation["device_ids"]) == 1
        assert "count" not in reservation, f"{service_name} must not use count: all"


@pytest.mark.parametrize(
    "profile",
    ("models-cpu", "models-single-gpu", "models-multi-gpu"),
)
def test_every_model_service_has_healthcheck_and_security_boundary(profile: str) -> None:
    services = _compose(profile)["services"]
    for service_name in MODEL_SERVICES & services.keys():
        service = services[service_name]
        assert service.get("healthcheck", {}).get("test"), f"{service_name} has no healthcheck"
        assert "no-new-privileges:true" in service.get("security_opt", [])
        assert "ALL" in service.get("cap_drop", [])


@pytest.mark.parametrize("profile", ("models-single-gpu", "models-multi-gpu"))
def test_custom_model_services_are_read_only(profile: str) -> None:
    services = _compose(profile)["services"]
    for service_name in CUSTOM_MODEL_SERVICES & services.keys():
        service = services[service_name]
        assert service.get("read_only") is True
        assert service.get("tmpfs"), f"{service_name} needs writable ephemeral /tmp"


def test_model_service_dockerfiles_run_as_non_root() -> None:
    for relative_path in (
        "docker/clap-service/Dockerfile",
        "docker/campplus-service/Dockerfile",
    ):
        dockerfile = PROJECT_ROOT / relative_path
        text = dockerfile.read_text(encoding="utf-8")
        from_lines = [
            line.split()[1]
            for line in text.splitlines()
            if line.strip().upper().startswith("FROM ")
        ]
        assert from_lines
        for image in from_lines:
            assert not image.endswith(":latest")
            assert "@" in image or ":" in image.rsplit("/", 1)[-1]
        user_lines = [
            line.strip() for line in text.splitlines() if line.strip().upper().startswith("USER ")
        ]
        assert user_lines, f"{relative_path} does not declare USER"
        assert user_lines[-1].split(maxsplit=1)[1] not in {"0", "root"}


def test_frontend_image_uses_the_committed_npm_lockfile() -> None:
    dockerfile = (PROJECT_ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY package.json package-lock.json ./" in dockerfile
    assert "RUN npm ci" in dockerfile
    assert "pnpm install" not in dockerfile

    frontend = _compose("mock")["services"]["frontend"]
    command = frontend["command"]
    rendered = " ".join(command) if isinstance(command, list) else command
    assert "npm run dev" in rendered
    assert "pnpm" not in rendered


def test_master_key_init_is_hardened_and_gates_backend_startup() -> None:
    services = _compose("mock")["services"]
    key_init = services["master-key-init"]

    assert key_init["network_mode"] == "none"
    assert key_init["read_only"] is True
    assert key_init["cap_drop"] == ["ALL"]
    assert set(key_init["cap_add"]) == {"CHOWN", "DAC_OVERRIDE", "FOWNER"}
    assert "no-new-privileges:true" in key_init["security_opt"]

    depends_on = services["backend"]["depends_on"]
    assert depends_on["master-key-init"]["condition"] == "service_completed_successfully"
    key_mounts = [
        volume
        for volume in services["backend"]["volumes"]
        if volume.get("target") == "/run/secrets"
    ]
    assert len(key_mounts) == 1
    assert key_mounts[0]["read_only"] is True


def test_backend_runtime_installs_physical_audio_tooling() -> None:
    dockerfile = (PROJECT_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    runtime = dockerfile.split("FROM python:3.13-slim AS runtime", maxsplit=1)[1]
    assert "ffmpeg" in runtime


def test_custom_model_builds_wire_the_requirements_context() -> None:
    services = _compose("models-single-gpu")["services"]
    expected_context = str(PROJECT_ROOT / "docker")
    for service_name in CUSTOM_MODEL_SERVICES:
        build = services[service_name]["build"]
        assert build["additional_contexts"]["model_files"] == expected_context
        dockerfile = (Path(build["context"]) / build["dockerfile"]).resolve()
        assert "COPY --from=model_files" in dockerfile.read_text(encoding="utf-8")


def test_backend_model_urls_use_container_ports() -> None:
    services = _compose("models-multi-gpu")["services"]
    environment = services["backend"]["environment"]
    assert environment["SILERO_VAD_URL"] == "http://silero-vad.invalid:8000"
    assert environment["BGE_M3_URL"] == "http://bge-m3:80"
    assert environment["OPENAI_BASE_URL_STRONG"] == "http://vllm-strong:8000/v1"
    assert environment["OPENAI_BASE_URL_WEAK"] == "http://vllm-weak:8000/v1"
    assert services["vllm-strong"]["environment"]["VLLM_API_KEY"] == "dummy"
    assert services["vllm-weak"]["environment"]["VLLM_API_KEY"] == "dummy"


def test_vllm_host_ports_do_not_shadow_backend() -> None:
    services = _compose("models-multi-gpu")["services"]
    backend_ports = {str(port["published"]) for port in services["backend"].get("ports", [])}
    for service_name in ("vllm-strong", "vllm-weak"):
        model_ports = {str(port["published"]) for port in services[service_name].get("ports", [])}
        assert backend_ports.isdisjoint(model_ports)


def test_optional_redis_is_ephemeral_bounded_and_not_a_startup_dependency() -> None:
    services = _compose("cache-redis")["services"]
    redis = services["redis"]
    command = " ".join(redis["command"])

    assert redis["image"] == "redis:8.8.0-alpine"
    assert redis.get("ports", []) == []
    assert redis["read_only"] is True
    assert redis["deploy"]["resources"]["limits"]["memory"] == "201326592"
    assert "--maxmemory 128mb" in command
    assert "--maxmemory-policy allkeys-lru" in command
    assert "--appendonly no" in command
    assert "--save " in command
    assert "redis" not in services["backend"].get("depends_on", {})
    assert services["backend"]["environment"]["LLM_HOT_CACHE_BACKEND"] == "auto"


def test_vllm_prefix_caching_is_enabled_for_strong_and_weak_models() -> None:
    services = _compose("models-multi-gpu")["services"]
    for service_name in ("vllm-strong", "vllm-weak"):
        command = services[service_name]["command"]
        rendered = " ".join(command) if isinstance(command, list) else command
        assert "--enable-prefix-caching" in rendered


def test_streaming_vad_override_mounts_local_model_read_only() -> None:
    _require_compose()
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "--file",
            str(COMPOSE_FILE),
            "--file",
            str(STREAMING_VAD_OVERRIDE),
            "--profile",
            "mock",
            "config",
            "--format",
            "json",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    backend = json.loads(result.stdout)["services"]["backend"]
    assert backend["environment"]["ADAPTER_STREAMING_VAD_MODE"] == "real"
    model_mounts = [
        volume for volume in backend["volumes"] if volume.get("target") == "/models/silero_vad.onnx"
    ]
    assert len(model_mounts) == 1
    assert model_mounts[0]["read_only"] is True
