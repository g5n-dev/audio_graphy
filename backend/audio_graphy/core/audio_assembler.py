"""Safe, auditable physical assembly of fragmented audio recordings.

The assembler treats every path and subprocess argument as untrusted input:

* sources and the relative output are confined to one canonical root;
* symbolic-link components, traversal, unsupported extensions, and resource
  limit violations are rejected before starting a process;
* ffprobe and ffmpeg are launched with ``create_subprocess_exec`` only;
* publication is an atomic ``os.replace`` from a private directory on the
  target filesystem, so failed or cancelled work never exposes a partial file.

Source files are opened read-only.  Their identity and size are revalidated
immediately before publication, and the returned manifest contains the hashes
and timeline offsets required for provenance.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

CommandMode = Literal["concat_copy", "transcode_pcm", "transcode_aac"]
PathInput = str | os.PathLike[str]

DEFAULT_INPUT_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
)
DEFAULT_OUTPUT_EXTENSIONS: Final[frozenset[str]] = frozenset({".aac", ".m4a", ".wav"})
_COPY_CODECS_BY_EXTENSION: Final[Mapping[str, frozenset[str]]] = {
    ".aac": frozenset({"aac"}),
    ".m4a": frozenset({"aac", "alac"}),
    ".wav": frozenset(
        {
            "pcm_alaw",
            "pcm_f32le",
            "pcm_f64le",
            "pcm_mulaw",
            "pcm_s16le",
            "pcm_s24le",
            "pcm_s32le",
            "pcm_u8",
        }
    ),
}


class AudioAssemblyError(Exception):
    """Base class for physical audio assembly failures."""


class AudioAssemblyValidationError(AudioAssemblyError, ValueError):
    """Raised before processing when a safety or input invariant is violated."""


class AudioAssemblyProcessError(AudioAssemblyError, RuntimeError):
    """Raised when ffprobe/ffmpeg fails or produces an invalid artifact."""


@dataclass(frozen=True, slots=True)
class AudioInputManifest:
    """Provenance record and merged-timeline position of one source."""

    path: str
    sha256: str
    size_bytes: int
    duration_sec: float
    timeline_start_sec: float
    timeline_end_sec: float
    codec: str
    sample_rate: int
    channels: int


@dataclass(frozen=True, slots=True)
class AudioAssemblyManifest:
    """Immutable result returned after an atomic publication."""

    output_path: str
    output_sha256: str
    output_bytes: int
    total_duration_sec: float
    command_mode: CommandMode
    inputs: tuple[AudioInputManifest, ...]


@dataclass(frozen=True, slots=True)
class _AudioMetadata:
    codec: str
    sample_rate: int
    channels: int
    duration_sec: float


@dataclass(frozen=True, slots=True)
class _ValidatedSource:
    path: Path
    relative_path: str
    size_bytes: int
    device: int
    inode: int
    modified_ns: int


class AudioAssembler:
    """Asynchronously concatenate local audio without weakening path safety."""

    def __init__(
        self,
        allowed_root: PathInput,
        *,
        allowed_input_extensions: frozenset[str] = DEFAULT_INPUT_EXTENSIONS,
        allowed_output_extensions: frozenset[str] = DEFAULT_OUTPUT_EXTENSIONS,
        max_sources: int = 128,
        max_total_bytes: int = 2 * 1024 * 1024 * 1024,
        transcode_sample_rate: int = 16_000,
        transcode_channels: Literal[1, 2] = 1,
        aac_bitrate: str = "96k",
        ffprobe_binary: str = "ffprobe",
        ffmpeg_binary: str = "ffmpeg",
        ffprobe_timeout_sec: float = 30.0,
        ffmpeg_timeout_sec: float = 15 * 60.0,
        max_concurrent_processes: int = 2,
        max_estimated_pcm_bytes: int = 2 * 1024 * 1024 * 1024,
        max_temporary_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> None:
        try:
            canonical_root = Path(allowed_root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AudioAssemblyValidationError("allowed root does not exist") from exc
        if not canonical_root.is_dir():
            raise AudioAssemblyValidationError("allowed root must be a directory")
        if max_sources <= 0:
            raise AudioAssemblyValidationError("max_sources must be positive")
        if max_total_bytes <= 0:
            raise AudioAssemblyValidationError("max_total_bytes must be positive")
        if transcode_sample_rate <= 0:
            raise AudioAssemblyValidationError("transcode_sample_rate must be positive")
        if transcode_channels not in (1, 2):
            raise AudioAssemblyValidationError("transcode_channels must be 1 or 2")
        if not aac_bitrate or any(character in aac_bitrate for character in "\0\r\n"):
            raise AudioAssemblyValidationError("aac_bitrate is invalid")
        if not ffprobe_binary or any(character in ffprobe_binary for character in "\0\r\n"):
            raise AudioAssemblyValidationError("ffprobe_binary is invalid")
        if not ffmpeg_binary or any(character in ffmpeg_binary for character in "\0\r\n"):
            raise AudioAssemblyValidationError("ffmpeg_binary is invalid")
        if not math.isfinite(ffprobe_timeout_sec) or ffprobe_timeout_sec <= 0:
            raise AudioAssemblyValidationError("ffprobe_timeout_sec must be positive")
        if not math.isfinite(ffmpeg_timeout_sec) or ffmpeg_timeout_sec <= 0:
            raise AudioAssemblyValidationError("ffmpeg_timeout_sec must be positive")
        if max_concurrent_processes <= 0:
            raise AudioAssemblyValidationError("max_concurrent_processes must be positive")
        if max_estimated_pcm_bytes <= 0:
            raise AudioAssemblyValidationError("max_estimated_pcm_bytes must be positive")
        if max_temporary_bytes <= 0:
            raise AudioAssemblyValidationError("max_temporary_bytes must be positive")

        self.allowed_root = canonical_root
        self.allowed_input_extensions = self._normalize_extensions(
            allowed_input_extensions,
            name="allowed_input_extensions",
        )
        self.allowed_output_extensions = self._normalize_extensions(
            allowed_output_extensions,
            name="allowed_output_extensions",
        )
        self.max_sources = max_sources
        self.max_total_bytes = max_total_bytes
        self.transcode_sample_rate = transcode_sample_rate
        self.transcode_channels = transcode_channels
        self.aac_bitrate = aac_bitrate
        self.ffprobe_binary = ffprobe_binary
        self.ffmpeg_binary = ffmpeg_binary
        self.ffprobe_timeout_sec = ffprobe_timeout_sec
        self.ffmpeg_timeout_sec = ffmpeg_timeout_sec
        self.max_estimated_pcm_bytes = max_estimated_pcm_bytes
        self.max_temporary_bytes = max_temporary_bytes
        self._process_slots = asyncio.Semaphore(max_concurrent_processes)

    async def assemble(
        self,
        sources: Sequence[PathInput],
        target_relative_path: PathInput,
    ) -> AudioAssemblyManifest:
        """Build and atomically publish one physical audio artifact.

        The order of ``sources`` is the merged timeline order.  Absolute source
        paths are accepted only when they remain inside ``allowed_root``;
        ``target_relative_path`` must always be relative.
        """
        validated_sources = self._validate_sources(sources)
        source_paths = frozenset(source.path for source in validated_sources)
        target = self._validate_target(target_relative_path, source_paths)

        probed: list[tuple[_ValidatedSource, _AudioMetadata, str]] = []
        for source in validated_sources:
            metadata = await self._probe(source.path)
            digest = await asyncio.to_thread(_sha256_file, source.path)
            probed.append((source, metadata, digest))

        input_manifest = self._build_input_manifest(probed)
        mode = self._select_mode(
            [metadata for _, metadata, _ in probed],
            target.suffix.lower(),
        )
        self._validate_processing_budget(
            validated_sources,
            [metadata for _, metadata, _ in probed],
            mode=mode,
        )

        self._create_and_revalidate_target_parent(target)
        target = self._validate_target(target_relative_path, source_paths)
        self._validate_temporary_disk_capacity(target.parent, validated_sources, probed, mode=mode)

        with tempfile.TemporaryDirectory(
            prefix=".audio-assembly-",
            dir=str(target.parent),
        ) as temporary_directory:
            temporary_root = Path(temporary_directory)
            concat_manifest = temporary_root / "sources.ffconcat"
            temporary_output = temporary_root / f"output{target.suffix.lower()}"
            _write_concat_manifest(concat_manifest, [source.path for source in validated_sources])

            command = self._build_ffmpeg_command(
                sources=[source.path for source in validated_sources],
                concat_manifest=concat_manifest,
                temporary_output=temporary_output,
                mode=mode,
            )
            await self._run_process(command, process_name="ffmpeg")

            output_size = _validated_output_size(
                temporary_output,
                max_bytes=self.max_temporary_bytes,
            )
            output_sha256 = await asyncio.to_thread(_sha256_file, temporary_output)

            self._revalidate_sources(validated_sources)
            target = self._validate_target(target_relative_path, source_paths)
            if target.parent != temporary_root.parent:
                raise AudioAssemblyValidationError("target parent changed during assembly")

            await asyncio.to_thread(_fsync_file, temporary_output)
            os.replace(temporary_output, target)
            _fsync_directory(target.parent)

        return AudioAssemblyManifest(
            output_path=target.relative_to(self.allowed_root).as_posix(),
            output_sha256=output_sha256,
            output_bytes=output_size,
            total_duration_sec=sum(item.duration_sec for item in input_manifest),
            command_mode=mode,
            inputs=input_manifest,
        )

    @staticmethod
    def _normalize_extensions(
        extensions: frozenset[str],
        *,
        name: str,
    ) -> frozenset[str]:
        normalized = frozenset(extension.lower() for extension in extensions)
        if not normalized or any(
            not extension.startswith(".")
            or len(extension) < 2
            or any(character in extension for character in "/\\\0\r\n")
            for extension in normalized
        ):
            raise AudioAssemblyValidationError(f"{name} contains an invalid extension")
        return normalized

    def _validate_sources(
        self,
        sources: Sequence[PathInput],
    ) -> tuple[_ValidatedSource, ...]:
        if isinstance(sources, (str, bytes, os.PathLike)):
            raise AudioAssemblyValidationError("sources must be a sequence of paths")
        if not sources:
            raise AudioAssemblyValidationError("at least one source is required")
        if len(sources) > self.max_sources:
            raise AudioAssemblyValidationError(f"at most {self.max_sources} sources are allowed")

        validated: list[_ValidatedSource] = []
        seen: set[Path] = set()
        total_bytes = 0
        for source_input in sources:
            raw = os.fspath(source_input)
            self._reject_control_characters(raw, label="source path")
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = self.allowed_root / candidate
            source = self._canonical_confined_path(candidate, label="source")
            if source in seen:
                raise AudioAssemblyValidationError("duplicate source paths are not allowed")
            seen.add(source)
            if source.suffix.lower() not in self.allowed_input_extensions:
                raise AudioAssemblyValidationError(
                    f"source extension is not allowed: {source.suffix or '<none>'}"
                )
            try:
                source_stat = os.stat(source, follow_symlinks=False)
            except OSError as exc:
                raise AudioAssemblyValidationError("source does not exist") from exc
            if not stat.S_ISREG(source_stat.st_mode):
                raise AudioAssemblyValidationError("source must be a regular file")
            if source_stat.st_size <= 0:
                raise AudioAssemblyValidationError("source must not be empty")

            total_bytes += source_stat.st_size
            if total_bytes > self.max_total_bytes:
                raise AudioAssemblyValidationError(
                    f"total source bytes exceed {self.max_total_bytes}"
                )
            validated.append(
                _ValidatedSource(
                    path=source,
                    relative_path=source.relative_to(self.allowed_root).as_posix(),
                    size_bytes=source_stat.st_size,
                    device=source_stat.st_dev,
                    inode=source_stat.st_ino,
                    modified_ns=source_stat.st_mtime_ns,
                )
            )
        return tuple(validated)

    def _validate_target(
        self,
        target_input: PathInput,
        source_paths: frozenset[Path],
    ) -> Path:
        raw = os.fspath(target_input)
        self._reject_control_characters(raw, label="target path")
        relative_target = Path(raw)
        if relative_target.is_absolute():
            raise AudioAssemblyValidationError("target path must be relative")
        if raw in ("", "."):
            raise AudioAssemblyValidationError("target path must name a file")

        target = self._canonical_confined_path(
            self.allowed_root / relative_target,
            label="target",
            require_exists=False,
        )
        if target.suffix.lower() not in self.allowed_output_extensions:
            raise AudioAssemblyValidationError(
                f"target extension is not allowed: {target.suffix or '<none>'}"
            )
        if target in source_paths:
            raise AudioAssemblyValidationError("target must not replace a source file")
        if target.exists():
            try:
                target_stat = os.stat(target, follow_symlinks=False)
            except OSError as exc:
                raise AudioAssemblyValidationError("target cannot be inspected") from exc
            if not stat.S_ISREG(target_stat.st_mode):
                raise AudioAssemblyValidationError("existing target must be a regular file")
            for source in source_paths:
                with contextlib.suppress(OSError):
                    if os.path.samefile(target, source):
                        raise AudioAssemblyValidationError("target must not replace a source file")
        return target

    def _canonical_confined_path(
        self,
        candidate: Path,
        *,
        label: str,
        require_exists: bool = True,
    ) -> Path:
        if candidate.is_symlink():
            raise AudioAssemblyValidationError(f"{label} path contains a symbolic link")

        try:
            resolved = candidate.resolve(strict=require_exists)
        except (OSError, RuntimeError) as exc:
            existence = " does not exist" if require_exists else " is invalid"
            raise AudioAssemblyValidationError(f"{label}{existence}") from exc
        if not resolved.is_relative_to(self.allowed_root):
            raise AudioAssemblyValidationError(f"{label} is outside allowed root")
        return resolved

    def _create_and_revalidate_target_parent(self, target: Path) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AudioAssemblyValidationError("target parent cannot be created") from exc
        self._canonical_confined_path(target.parent, label="target parent")

    @staticmethod
    def _reject_control_characters(raw_path: str, *, label: str) -> None:
        if any(character in raw_path for character in "\0\r\n"):
            raise AudioAssemblyValidationError(f"{label} contains a control character")

    async def _probe(self, source: Path) -> _AudioMetadata:
        command = [
            self.ffprobe_binary,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,duration:format=duration",
            "-of",
            "json",
            str(source),
        ]
        stdout, _ = await self._run_process(command, process_name="ffprobe")
        try:
            payload_object: object = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AudioAssemblyProcessError("ffprobe returned invalid JSON") from exc
        if not isinstance(payload_object, dict):
            raise AudioAssemblyProcessError("ffprobe returned an invalid payload")

        payload = cast(Mapping[str, object], payload_object)
        streams_object = payload.get("streams")
        if not isinstance(streams_object, list) or not streams_object:
            raise AudioAssemblyProcessError("ffprobe found no audio stream")
        stream_object = streams_object[0]
        if not isinstance(stream_object, dict):
            raise AudioAssemblyProcessError("ffprobe returned invalid stream metadata")
        stream = cast(Mapping[str, object], stream_object)

        codec_object = stream.get("codec_name")
        if not isinstance(codec_object, str) or not codec_object.strip():
            raise AudioAssemblyProcessError("ffprobe returned an invalid codec")
        codec = codec_object.strip().lower()
        sample_rate = _positive_int(stream.get("sample_rate"), label="sample rate")
        channels = _positive_int(stream.get("channels"), label="channel count")

        duration: float | None = _finite_positive_float(stream.get("duration"))
        if duration is None:
            format_object = payload.get("format")
            if isinstance(format_object, dict):
                audio_format = cast(Mapping[str, object], format_object)
                duration = _finite_positive_float(audio_format.get("duration"))
        if duration is None:
            raise AudioAssemblyProcessError("ffprobe returned an invalid duration")
        return _AudioMetadata(
            codec=codec,
            sample_rate=sample_rate,
            channels=channels,
            duration_sec=duration,
        )

    async def _run_process(
        self,
        command: Sequence[str],
        *,
        process_name: str,
    ) -> tuple[bytes, bytes]:
        timeout_sec = (
            self.ffprobe_timeout_sec if process_name == "ffprobe" else self.ffmpeg_timeout_sec
        )
        async with self._process_slots:
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                raise AudioAssemblyProcessError(f"{process_name} could not be started") from exc

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout_sec,
                )
            except TimeoutError as exc:
                await self._terminate_process(process)
                raise AudioAssemblyProcessError(
                    f"{process_name} timed out after {timeout_sec:g} seconds"
                ) from exc
            except BaseException:
                await self._terminate_process(process)
                raise

        if process.returncode != 0:
            error_text = stderr.decode("utf-8", errors="replace").strip()
            if len(error_text) > 2_000:
                error_text = f"{error_text[:2_000]}…"
            detail = f": {error_text}" if error_text else ""
            raise AudioAssemblyProcessError(
                f"{process_name} exited with code {process.returncode}{detail}"
            )
        return stdout, stderr

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        with contextlib.suppress(Exception):
            await process.wait()

    def _validate_processing_budget(
        self,
        sources: Sequence[_ValidatedSource],
        metadata: Sequence[_AudioMetadata],
        *,
        mode: CommandMode,
    ) -> None:
        estimated_pcm_bytes = self._estimated_pcm_bytes(metadata)
        if mode != "concat_copy" and estimated_pcm_bytes > self.max_estimated_pcm_bytes:
            raise AudioAssemblyValidationError(
                f"estimated decoded PCM exceeds PCM budget {self.max_estimated_pcm_bytes}"
            )
        estimated_temporary_bytes = self._estimated_temporary_bytes(
            sources,
            estimated_pcm_bytes,
            mode=mode,
        )
        if estimated_temporary_bytes > self.max_temporary_bytes:
            raise AudioAssemblyValidationError(
                f"estimated output exceeds temporary-file budget {self.max_temporary_bytes}"
            )

    def _validate_temporary_disk_capacity(
        self,
        target_parent: Path,
        sources: Sequence[_ValidatedSource],
        probed: Sequence[tuple[_ValidatedSource, _AudioMetadata, str]],
        *,
        mode: CommandMode,
    ) -> None:
        estimated_pcm_bytes = self._estimated_pcm_bytes([metadata for _, metadata, _ in probed])
        required_bytes = self._estimated_temporary_bytes(
            sources,
            estimated_pcm_bytes,
            mode=mode,
        )
        try:
            free_bytes = shutil.disk_usage(target_parent).free
        except OSError as exc:
            raise AudioAssemblyValidationError(
                "temporary disk capacity cannot be inspected"
            ) from exc
        if required_bytes > free_bytes:
            raise AudioAssemblyValidationError(
                f"insufficient temporary disk capacity: need {required_bytes}, have {free_bytes}"
            )

    def _estimated_pcm_bytes(self, metadata: Sequence[_AudioMetadata]) -> int:
        total_duration_sec = sum(item.duration_sec for item in metadata)
        return (
            math.ceil(total_duration_sec * self.transcode_sample_rate * self.transcode_channels * 2)
            + 4096
        )

    @staticmethod
    def _estimated_temporary_bytes(
        sources: Sequence[_ValidatedSource],
        estimated_pcm_bytes: int,
        *,
        mode: CommandMode,
    ) -> int:
        if mode == "concat_copy":
            return sum(source.size_bytes for source in sources) + 4096
        # PCM is exact enough for WAV and a conservative disk bound for AAC.
        return estimated_pcm_bytes

    def _select_mode(
        self,
        metadata: Sequence[_AudioMetadata],
        target_extension: str,
    ) -> CommandMode:
        signatures = {(item.codec, item.sample_rate, item.channels) for item in metadata}
        codec = metadata[0].codec
        copy_codecs = _COPY_CODECS_BY_EXTENSION.get(target_extension, frozenset())
        if len(signatures) == 1 and codec in copy_codecs:
            return "concat_copy"
        if target_extension == ".wav":
            return "transcode_pcm"
        return "transcode_aac"

    def _build_ffmpeg_command(
        self,
        *,
        sources: Sequence[Path],
        concat_manifest: Path,
        temporary_output: Path,
        mode: CommandMode,
    ) -> list[str]:
        base_command = [
            self.ffmpeg_binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
        ]
        if mode == "concat_copy":
            return [
                *base_command,
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_manifest),
                "-map",
                "0:a:0",
                "-c:a",
                "copy",
                str(temporary_output),
            ]

        for source in sources:
            base_command.extend(("-i", str(source)))
        channel_layout = "mono" if self.transcode_channels == 1 else "stereo"
        normalized_labels: list[str] = []
        filter_parts: list[str] = []
        for index in range(len(sources)):
            label = f"a{index}"
            normalized_labels.append(f"[{label}]")
            filter_parts.append(
                f"[{index}:a:0]"
                f"aresample={self.transcode_sample_rate},"
                f"aformat=sample_rates={self.transcode_sample_rate}:"
                f"channel_layouts={channel_layout}"
                f"[{label}]"
            )
        filter_parts.append("".join(normalized_labels) + f"concat=n={len(sources)}:v=0:a=1[outa]")
        base_command.extend(
            (
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                "[outa]",
                "-ar",
                str(self.transcode_sample_rate),
                "-ac",
                str(self.transcode_channels),
            )
        )
        if mode == "transcode_pcm":
            base_command.extend(("-c:a", "pcm_s16le"))
        else:
            base_command.extend(("-c:a", "aac", "-b:a", self.aac_bitrate))
            if temporary_output.suffix.lower() == ".m4a":
                base_command.extend(("-movflags", "+faststart"))
        base_command.append(str(temporary_output))
        return base_command

    @staticmethod
    def _build_input_manifest(
        probed: Sequence[tuple[_ValidatedSource, _AudioMetadata, str]],
    ) -> tuple[AudioInputManifest, ...]:
        timeline_cursor = 0.0
        manifest: list[AudioInputManifest] = []
        for source, metadata, digest in probed:
            timeline_end = timeline_cursor + metadata.duration_sec
            manifest.append(
                AudioInputManifest(
                    path=source.relative_path,
                    sha256=digest,
                    size_bytes=source.size_bytes,
                    duration_sec=metadata.duration_sec,
                    timeline_start_sec=timeline_cursor,
                    timeline_end_sec=timeline_end,
                    codec=metadata.codec,
                    sample_rate=metadata.sample_rate,
                    channels=metadata.channels,
                )
            )
            timeline_cursor = timeline_end
        return tuple(manifest)

    @staticmethod
    def _revalidate_sources(sources: Sequence[_ValidatedSource]) -> None:
        for source in sources:
            try:
                source_stat = os.stat(source.path, follow_symlinks=False)
            except OSError as exc:
                raise AudioAssemblyValidationError(
                    "source changed or disappeared during assembly"
                ) from exc
            current_identity = (
                source_stat.st_dev,
                source_stat.st_ino,
                source_stat.st_size,
                source_stat.st_mtime_ns,
            )
            expected_identity = (
                source.device,
                source.inode,
                source.size_bytes,
                source.modified_ns,
            )
            if not stat.S_ISREG(source_stat.st_mode) or current_identity != expected_identity:
                raise AudioAssemblyValidationError("source changed or disappeared during assembly")


def _positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool):
        raise AudioAssemblyProcessError(f"ffprobe returned an invalid {label}")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, (str, bytes, bytearray)):
        try:
            parsed = int(value)
        except ValueError as exc:
            raise AudioAssemblyProcessError(f"ffprobe returned an invalid {label}") from exc
    else:
        raise AudioAssemblyProcessError(f"ffprobe returned an invalid {label}")
    if parsed <= 0:
        raise AudioAssemblyProcessError(f"ffprobe returned an invalid {label}")
    return parsed


def _finite_positive_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (str, int, float)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as audio_file:
        while chunk := audio_file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_concat_manifest(manifest_path: Path, sources: Sequence[Path]) -> None:
    lines = ["ffconcat version 1.0"]
    for source in sources:
        raw_path = str(source)
        if any(character in raw_path for character in "\0\r\n"):
            raise AudioAssemblyValidationError(
                "source path cannot be represented safely in concat manifest"
            )
        escaped_path = raw_path.replace("'", "'\\''")
        lines.append(f"file '{escaped_path}'")
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _validated_output_size(output_path: Path, *, max_bytes: int) -> int:
    try:
        output_stat = os.stat(output_path, follow_symlinks=False)
    except OSError as exc:
        raise AudioAssemblyProcessError("ffmpeg did not create an output file") from exc
    if not stat.S_ISREG(output_stat.st_mode) or output_stat.st_size <= 0:
        raise AudioAssemblyProcessError("ffmpeg produced an empty or invalid output file")
    if output_stat.st_size > max_bytes:
        raise AudioAssemblyProcessError(f"ffmpeg output exceeds temporary-file budget {max_bytes}")
    return output_stat.st_size


def _fsync_file(path: Path) -> None:
    with path.open("rb") as output:
        os.fsync(output.fileno())


def _fsync_directory(directory: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)
