"""Security and provenance tests for physical audio assembly."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from audio_graphy.core.audio_assembler import (
    AudioAssembler,
    AudioAssemblyProcessError,
    AudioAssemblyValidationError,
)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    codec: str = "pcm_s16le"
    sample_rate: int = 16_000
    channels: int = 1
    duration_sec: float = 1.0


class FakeProcess:
    """Small asyncio subprocess double with controllable cancellation."""

    def __init__(
        self,
        owner: FakeSubprocessFactory,
        args: tuple[str, ...],
    ) -> None:
        self._owner = owner
        self.args = args
        self.returncode: int | None = None
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._is_ffprobe():
            if self._owner.block_ffprobe:
                self._owner.ffprobe_started.set()
                await self._owner.release_ffprobe.wait()
            return self._probe_response()

        self._owner.ffmpeg_commands.append(self.args)
        self._capture_concat_manifest()

        if self._owner.block_ffmpeg:
            self._owner.ffmpeg_started.set()
            await self._owner.release_ffmpeg.wait()

        return self._ffmpeg_response()

    def _is_ffprobe(self) -> bool:
        return Path(self.args[0]).name == Path(self._owner.ffprobe_binary).name

    def _probe_response(self) -> tuple[bytes, bytes]:
        source = Path(self.args[-1]).resolve()
        probe = self._owner.probes[source]
        self.returncode = self._owner.ffprobe_returncode
        if self.returncode != 0:
            return b"", self._owner.ffprobe_stderr
        if self._owner.probe_payload is not None:
            return self._owner.probe_payload, b""
        payload = {
            "streams": [
                {
                    "codec_name": probe.codec,
                    "sample_rate": str(probe.sample_rate),
                    "channels": probe.channels,
                    "duration": str(probe.duration_sec),
                }
            ],
            "format": {"duration": str(probe.duration_sec)},
        }
        return json.dumps(payload).encode(), b""

    def _capture_concat_manifest(self) -> None:
        if "-f" in self.args and self.args[self.args.index("-f") + 1] == "concat":
            manifest_path = Path(self.args[self.args.index("-i") + 1])
            self._owner.concat_manifests.append(manifest_path.read_text(encoding="utf-8"))

    def _ffmpeg_response(self) -> tuple[bytes, bytes]:
        output_path = Path(self.args[-1])
        if self._owner.ffmpeg_output is not None:
            output_path.write_bytes(self._owner.ffmpeg_output)
        if self._owner.on_ffmpeg is not None:
            self._owner.on_ffmpeg()
        self.returncode = self._owner.ffmpeg_returncode
        return b"", self._owner.ffmpeg_stderr

    def kill(self) -> None:
        self.killed = True
        self._owner.killed_processes.append(self)
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        return self.returncode if self.returncode is not None else 0


class FakeSubprocessFactory:
    """Captures exec-style argv and emulates ffprobe/ffmpeg."""

    def __init__(
        self,
        probes: Mapping[Path, ProbeResult],
        *,
        ffprobe_binary: str = "ffprobe",
        ffmpeg_returncode: int = 0,
        ffmpeg_output: bytes | None = b"assembled-audio",
        block_ffprobe: bool = False,
        block_ffmpeg: bool = False,
        probe_payload: bytes | None = None,
        on_ffmpeg: Callable[[], None] | None = None,
    ) -> None:
        self.probes = {path.resolve(): value for path, value in probes.items()}
        self.ffprobe_binary = ffprobe_binary
        self.ffprobe_returncode = 0
        self.ffprobe_stderr = b"probe failed"
        self.ffmpeg_returncode = ffmpeg_returncode
        self.ffmpeg_output = ffmpeg_output
        self.ffmpeg_stderr = b"ffmpeg failed"
        self.block_ffprobe = block_ffprobe
        self.block_ffmpeg = block_ffmpeg
        self.probe_payload = probe_payload
        self.on_ffmpeg = on_ffmpeg
        self.ffmpeg_started = asyncio.Event()
        self.release_ffmpeg = asyncio.Event()
        self.ffprobe_started = asyncio.Event()
        self.release_ffprobe = asyncio.Event()
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.ffmpeg_commands: list[tuple[str, ...]] = []
        self.concat_manifests: list[str] = []
        self.killed_processes: list[FakeProcess] = []

    async def __call__(self, *args: str, **kwargs: Any) -> FakeProcess:
        self.calls.append((args, kwargs))
        return FakeProcess(self, args)


def _write_audio(root: Path, relative_path: str, payload: bytes = b"source-audio") -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _install_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    probes: Mapping[Path, ProbeResult],
    **kwargs: Any,
) -> FakeSubprocessFactory:
    factory = FakeSubprocessFactory(probes, **kwargs)
    monkeypatch.setattr(
        "audio_graphy.core.audio_assembler.asyncio.create_subprocess_exec",
        factory,
    )
    return factory


class TestPathAndResourceBoundaries:
    def test_rejects_invalid_configuration(self, tmp_path: Path) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        file_root = _write_audio(tmp_path, "not-a-directory.wav")
        invalid_configurations: list[tuple[Path, dict[str, Any]]] = [
            (tmp_path / "missing", {}),
            (file_root, {}),
            (root, {"max_sources": 0}),
            (root, {"max_total_bytes": 0}),
            (root, {"transcode_sample_rate": 0}),
            (root, {"transcode_channels": 3}),
            (root, {"aac_bitrate": ""}),
            (root, {"ffprobe_binary": "bad\nbinary"}),
            (root, {"ffmpeg_binary": ""}),
            (root, {"ffprobe_timeout_sec": 0}),
            (root, {"ffmpeg_timeout_sec": 0}),
            (root, {"max_concurrent_processes": 0}),
            (root, {"max_estimated_pcm_bytes": 0}),
            (root, {"max_temporary_bytes": 0}),
            (root, {"allowed_input_extensions": frozenset({"wav"})}),
            (root, {"allowed_output_extensions": frozenset()}),
        ]

        for allowed_root, kwargs in invalid_configurations:
            with pytest.raises(AudioAssemblyValidationError):
                AudioAssembler(allowed_root, **kwargs)

    @pytest.mark.parametrize(
        "target",
        (
            "../outside.wav",
            "nested/../../outside.wav",
            "/tmp/absolute.wav",
        ),
    )
    async def test_rejects_traversal_and_absolute_targets(
        self,
        tmp_path: Path,
        target: str,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.wav")

        with pytest.raises(AudioAssemblyValidationError):
            await AudioAssembler(root).assemble([source], target)

        assert source.read_bytes() == b"source-audio"

    async def test_rejects_source_outside_allowed_root(self, tmp_path: Path) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        outside = _write_audio(tmp_path, "outside.wav")

        with pytest.raises(AudioAssemblyValidationError, match="allowed root"):
            await AudioAssembler(root).assemble([outside], "merged.wav")

    async def test_allowed_root_alias_resolves_to_the_same_security_boundary(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        canonical_root = tmp_path / "canonical-audio"
        canonical_root.mkdir()
        root_alias = tmp_path / "audio-alias"
        root_alias.symlink_to(canonical_root, target_is_directory=True)
        source = _write_audio(root_alias, "source.wav")
        _install_subprocess(monkeypatch, {source: ProbeResult()})

        result = await AudioAssembler(root_alias).assemble(
            [source],
            "merged.wav",
        )

        assert result.output_path == "merged.wav"
        assert (canonical_root / "merged.wav").is_file()

    async def test_rejects_symlink_source_escape(self, tmp_path: Path) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        outside = _write_audio(tmp_path, "outside.wav")
        link = root / "escape.wav"
        link.symlink_to(outside)

        with pytest.raises(AudioAssemblyValidationError, match=r"symbolic link|allowed root"):
            await AudioAssembler(root).assemble([link], "merged.wav")

        assert outside.read_bytes() == b"source-audio"

    async def test_rejects_symlink_target_parent_escape(self, tmp_path: Path) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.wav")
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (root / "escaped").symlink_to(outside_dir, target_is_directory=True)

        with pytest.raises(AudioAssemblyValidationError, match=r"symbolic link|allowed root"):
            await AudioAssembler(root).assemble([source], "escaped/merged.wav")

        assert not (outside_dir / "merged.wav").exists()

    async def test_rejects_output_that_would_replace_a_source(self, tmp_path: Path) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.wav")

        with pytest.raises(AudioAssemblyValidationError, match="source"):
            await AudioAssembler(root).assemble([source], "source.wav")

        assert source.read_bytes() == b"source-audio"

    async def test_rejects_hard_link_output_that_aliases_a_source(self, tmp_path: Path) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.wav")
        target = root / "merged.wav"
        os.link(source, target)

        with pytest.raises(AudioAssemblyValidationError, match="source"):
            await AudioAssembler(root).assemble([source], "merged.wav")

        assert source.read_bytes() == b"source-audio"

    async def test_rejects_existing_output_symlink(self, tmp_path: Path) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.wav")
        outside = _write_audio(tmp_path, "outside.wav", b"do-not-replace")
        (root / "merged.wav").symlink_to(outside)

        with pytest.raises(AudioAssemblyValidationError, match="symbolic link"):
            await AudioAssembler(root).assemble([source], "merged.wav")

        assert outside.read_bytes() == b"do-not-replace"

    async def test_enforces_extension_count_and_total_size_limits(self, tmp_path: Path) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        first = _write_audio(root, "first.wav", b"1234")
        second = _write_audio(root, "second.wav", b"5678")
        disallowed = _write_audio(root, "payload.txt", b"audio?")

        with pytest.raises(AudioAssemblyValidationError, match="extension"):
            await AudioAssembler(root).assemble([disallowed], "merged.wav")
        with pytest.raises(AudioAssemblyValidationError, match="at most 1"):
            await AudioAssembler(root, max_sources=1).assemble(
                [first, second],
                "merged.wav",
            )
        with pytest.raises(AudioAssemblyValidationError, match="total source bytes"):
            await AudioAssembler(root, max_total_bytes=7).assemble(
                [first, second],
                "merged.wav",
            )

    async def test_rejects_empty_duplicate_missing_and_non_regular_sources(
        self,
        tmp_path: Path,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.wav")
        empty = _write_audio(root, "empty.wav", b"")
        directory = root / "directory.wav"
        directory.mkdir()
        assembler = AudioAssembler(root)
        invalid_sources: list[Any] = [
            "source.wav",
            [],
            [source, source],
            [empty],
            [root / "missing.wav"],
            [directory],
        ]

        for sources in invalid_sources:
            with pytest.raises(AudioAssemblyValidationError):
                await assembler.assemble(sources, "merged.wav")

    @pytest.mark.parametrize(
        ("sources", "target"),
        (
            (["bad\nname.wav"], "merged.wav"),
            (["source.wav"], "bad\nname.wav"),
            (["source.wav"], ""),
            (["source.wav"], "merged.mp3"),
        ),
    )
    async def test_rejects_control_characters_empty_target_and_output_extension(
        self,
        tmp_path: Path,
        sources: list[str],
        target: str,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        _write_audio(root, "source.wav")

        with pytest.raises(AudioAssemblyValidationError):
            await AudioAssembler(root).assemble(sources, target)

    async def test_rejects_existing_output_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.wav")
        (root / "merged.wav").mkdir()

        with pytest.raises(AudioAssemblyValidationError, match="regular file"):
            await AudioAssembler(root).assemble([source], "merged.wav")


class TestCommandsAndFormatSelection:
    async def test_same_format_uses_safe_concat_copy_and_escaped_manifest(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        first = _write_audio(root, "first.wav", b"first")
        injected = _write_audio(root, "clip 'quoted'; touch PWNED.wav", b"second")
        fake = _install_subprocess(
            monkeypatch,
            {
                first: ProbeResult(duration_sec=1.25),
                injected: ProbeResult(duration_sec=2.75),
            },
        )

        result = await AudioAssembler(root).assemble(
            [first, injected],
            "merged.wav",
        )

        command = fake.ffmpeg_commands[0]
        assert result.command_mode == "concat_copy"
        assert command[0] == "ffmpeg"
        assert command[command.index("-c:a") + 1] == "copy"
        assert command[command.index("-f") + 1] == "concat"
        assert "-safe" in command
        assert not any("shell" in kwargs for _, kwargs in fake.calls)
        assert str(injected.resolve()) not in command
        assert len(fake.concat_manifests) == 1
        assert "'\\''" in fake.concat_manifests[0]
        assert "; touch PWNED.wav" in fake.concat_manifests[0]
        assert not (root / "PWNED.wav").exists()

    async def test_different_metadata_uses_explicit_pcm_resampling_for_wav(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        first = _write_audio(root, "first.wav")
        second = _write_audio(root, "second.flac")
        fake = _install_subprocess(
            monkeypatch,
            {
                first: ProbeResult(codec="pcm_s16le", sample_rate=16_000, channels=1),
                second: ProbeResult(codec="flac", sample_rate=48_000, channels=2),
            },
        )

        result = await AudioAssembler(root).assemble(
            [first, second],
            "merged.wav",
        )

        command = fake.ffmpeg_commands[0]
        filter_graph = command[command.index("-filter_complex") + 1]
        assert result.command_mode == "transcode_pcm"
        assert command[command.index("-c:a") + 1] == "pcm_s16le"
        assert command[command.index("-ar") + 1] == "16000"
        assert command[command.index("-ac") + 1] == "1"
        assert "aresample=16000" in filter_graph
        assert "concat=n=2:v=0:a=1" in filter_graph
        assert command.count("-i") == 2

    async def test_different_metadata_uses_explicit_aac_resampling_for_m4a(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        first = _write_audio(root, "first.wav")
        second = _write_audio(root, "second.flac")
        fake = _install_subprocess(
            monkeypatch,
            {
                first: ProbeResult(codec="pcm_s16le"),
                second: ProbeResult(codec="flac", sample_rate=44_100, channels=2),
            },
        )

        result = await AudioAssembler(root).assemble(
            [first, second],
            "merged.m4a",
        )

        command = fake.ffmpeg_commands[0]
        assert result.command_mode == "transcode_aac"
        assert command[command.index("-c:a") + 1] == "aac"
        assert command[command.index("-b:a") + 1] == "96k"
        assert command[command.index("-movflags") + 1] == "+faststart"

    async def test_aac_target_and_stereo_configuration_are_explicit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.flac")
        fake = _install_subprocess(
            monkeypatch,
            {source: ProbeResult(codec="flac")},
        )

        result = await AudioAssembler(root, transcode_channels=2).assemble(
            [source],
            "merged.aac",
        )

        command = fake.ffmpeg_commands[0]
        filter_graph = command[command.index("-filter_complex") + 1]
        assert result.command_mode == "transcode_aac"
        assert "channel_layouts=stereo" in filter_graph
        assert command[command.index("-ac") + 1] == "2"
        assert "-movflags" not in command


class TestAtomicityCancellationAndProvenance:
    async def test_returns_hashes_durations_and_timeline_offsets(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        first = _write_audio(root, "first.wav", b"first-source")
        second = _write_audio(root, "second.wav", b"second-source")
        output = b"finished-output"
        _install_subprocess(
            monkeypatch,
            {
                first: ProbeResult(duration_sec=1.25),
                second: ProbeResult(duration_sec=2.5),
            },
            ffmpeg_output=output,
        )

        result = await AudioAssembler(root).assemble(
            ["first.wav", "second.wav"],
            "assembled/merged.wav",
        )

        assert result.output_path == "assembled/merged.wav"
        assert result.output_sha256 == hashlib.sha256(output).hexdigest()
        assert result.output_bytes == len(output)
        assert result.total_duration_sec == pytest.approx(3.75)
        assert [item.sha256 for item in result.inputs] == [
            hashlib.sha256(b"first-source").hexdigest(),
            hashlib.sha256(b"second-source").hexdigest(),
        ]
        assert [
            (item.timeline_start_sec, item.timeline_end_sec) for item in result.inputs
        ] == pytest.approx([(0.0, 1.25), (1.25, 3.75)])

    async def test_success_atomically_replaces_target_and_preserves_sources(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        first = _write_audio(root, "first.wav", b"immutable-first")
        second = _write_audio(root, "second.wav", b"immutable-second")
        target = _write_audio(root, "merged.wav", b"old-output")
        _install_subprocess(
            monkeypatch,
            {first: ProbeResult(), second: ProbeResult()},
            ffmpeg_output=b"new-output",
        )
        real_replace = os.replace
        replacements: list[tuple[Path, Path]] = []

        def recording_replace(
            source: str | os.PathLike[str], destination: str | os.PathLike[str]
        ) -> None:
            replacements.append((Path(source), Path(destination)))
            real_replace(source, destination)

        monkeypatch.setattr(
            "audio_graphy.core.audio_assembler.os.replace",
            recording_replace,
        )

        await AudioAssembler(root).assemble([first, second], "merged.wav")

        assert len(replacements) == 1
        temp_output, published_output = replacements[0]
        assert temp_output.parent != root
        assert published_output == target
        assert target.read_bytes() == b"new-output"
        assert first.read_bytes() == b"immutable-first"
        assert second.read_bytes() == b"immutable-second"

    @pytest.mark.parametrize(
        ("returncode", "output"),
        (
            (1, b"partial-output"),
            (0, None),
            (0, b""),
        ),
    )
    async def test_failure_never_publishes_partial_output_and_cleans_temp_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        returncode: int,
        output: bytes | None,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.wav", b"immutable-source")
        target = _write_audio(root, "merged.wav", b"old-output")
        _install_subprocess(
            monkeypatch,
            {source: ProbeResult()},
            ffmpeg_returncode=returncode,
            ffmpeg_output=output,
        )

        with pytest.raises(AudioAssemblyProcessError):
            await AudioAssembler(root).assemble([source], "merged.wav")

        assert target.read_bytes() == b"old-output"
        assert source.read_bytes() == b"immutable-source"
        assert not list(root.glob(".audio-assembly-*"))

    async def test_cancellation_kills_child_and_cleans_without_touching_sources(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.wav", b"immutable-source")
        fake = _install_subprocess(
            monkeypatch,
            {source: ProbeResult()},
            block_ffmpeg=True,
        )

        task = asyncio.create_task(
            AudioAssembler(root).assemble([source], "merged.wav"),
        )
        await asyncio.wait_for(fake.ffmpeg_started.wait(), timeout=1)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(fake.killed_processes) == 1
        assert fake.killed_processes[0].waited is True
        assert source.read_bytes() == b"immutable-source"
        assert not (root / "merged.wav").exists()
        assert not list(root.glob(".audio-assembly-*"))

    async def test_ffmpeg_timeout_kills_child_and_cleans_temp_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.wav", b"immutable-source")
        fake = _install_subprocess(
            monkeypatch,
            {source: ProbeResult()},
            block_ffmpeg=True,
        )

        with pytest.raises(AudioAssemblyProcessError, match="timed out"):
            await AudioAssembler(root, ffmpeg_timeout_sec=0.01).assemble(
                [source],
                "merged.wav",
            )

        assert len(fake.killed_processes) == 1
        assert fake.killed_processes[0].waited is True
        assert not (root / "merged.wav").exists()
        assert not list(root.glob(".audio-assembly-*"))

    async def test_ffprobe_timeout_kills_child_before_ffmpeg(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.wav", b"immutable-source")
        fake = _install_subprocess(
            monkeypatch,
            {source: ProbeResult()},
            block_ffprobe=True,
        )

        with pytest.raises(AudioAssemblyProcessError, match="ffprobe timed out"):
            await AudioAssembler(root, ffprobe_timeout_sec=0.01).assemble(
                [source],
                "merged.wav",
            )

        assert len(fake.killed_processes) == 1
        assert fake.killed_processes[0].waited is True
        assert not fake.ffmpeg_commands

    async def test_process_concurrency_is_bounded_per_assembler(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        first = _write_audio(root, "first.wav")
        second = _write_audio(root, "second.wav")
        fake = _install_subprocess(
            monkeypatch,
            {
                first: ProbeResult(),
                second: ProbeResult(),
            },
            block_ffmpeg=True,
        )
        assembler = AudioAssembler(root, max_concurrent_processes=1)
        first_task = asyncio.create_task(assembler.assemble([first], "first-merged.wav"))
        await asyncio.wait_for(fake.ffmpeg_started.wait(), timeout=1)

        second_task = asyncio.create_task(assembler.assemble([second], "second-merged.wav"))
        await asyncio.sleep(0)
        assert len(fake.calls) == 2  # first probe + first ffmpeg; second probe is queued

        fake.release_ffmpeg.set()
        await asyncio.gather(first_task, second_task)
        assert (root / "first-merged.wav").exists()
        assert (root / "second-merged.wav").exists()

    async def test_estimated_pcm_budget_rejects_before_ffmpeg(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.flac")
        fake = _install_subprocess(
            monkeypatch,
            {source: ProbeResult(codec="flac", duration_sec=60.0)},
        )

        with pytest.raises(AudioAssemblyValidationError, match="PCM budget"):
            await AudioAssembler(root, max_estimated_pcm_bytes=1024).assemble(
                [source],
                "merged.wav",
            )

        assert not fake.ffmpeg_commands

    async def test_actual_output_over_temporary_budget_is_never_published(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.wav")
        target = _write_audio(root, "merged.wav", b"old-output")
        _install_subprocess(
            monkeypatch,
            {source: ProbeResult(duration_sec=0.001)},
            ffmpeg_output=b"x" * 5001,
        )

        with pytest.raises(AudioAssemblyProcessError, match="temporary-file budget"):
            await AudioAssembler(root, max_temporary_bytes=5000).assemble(
                [source],
                "merged.wav",
            )

        assert target.read_bytes() == b"old-output"

    async def test_probe_failure_is_reported_before_ffmpeg_runs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.wav")
        fake = _install_subprocess(monkeypatch, {source: ProbeResult()})
        fake.ffprobe_returncode = 1

        with pytest.raises(AudioAssemblyProcessError, match="ffprobe"):
            await AudioAssembler(root).assemble([source], "merged.wav")

        assert not fake.ffmpeg_commands

    @pytest.mark.parametrize(
        "payload",
        (
            b"{not-json",
            b"[]",
            b'{"streams":[]}',
            b'{"streams":[[]]}',
            b'{"streams":[{"codec_name":"","sample_rate":"16000","channels":1,"duration":"1"}]}',
            b'{"streams":[{"codec_name":"pcm_s16le","sample_rate":"bad","channels":1,"duration":"1"}]}',
            b'{"streams":[{"codec_name":"pcm_s16le","sample_rate":"16000","channels":0,"duration":"1"}]}',
            b'{"streams":[{"codec_name":"pcm_s16le","sample_rate":"16000","channels":1,"duration":"nan"}]}',
        ),
    )
    async def test_rejects_malformed_probe_metadata(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        payload: bytes,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.wav")
        _install_subprocess(
            monkeypatch,
            {source: ProbeResult()},
            probe_payload=payload,
        )

        with pytest.raises(AudioAssemblyProcessError, match="ffprobe"):
            await AudioAssembler(root).assemble([source], "merged.wav")

    async def test_uses_format_duration_when_stream_duration_is_unavailable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.wav")
        payload = json.dumps(
            {
                "streams": [
                    {
                        "codec_name": "pcm_s16le",
                        "sample_rate": "16000",
                        "channels": 1,
                        "duration": "N/A",
                    }
                ],
                "format": {"duration": "2.25"},
            }
        ).encode()
        _install_subprocess(
            monkeypatch,
            {source: ProbeResult()},
            probe_payload=payload,
        )

        result = await AudioAssembler(root).assemble([source], "merged.wav")

        assert result.total_duration_sec == pytest.approx(2.25)

    async def test_process_start_failure_is_wrapped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.wav")

        async def failing_exec(*args: str, **kwargs: Any) -> FakeProcess:
            raise OSError("executable missing")

        monkeypatch.setattr(
            "audio_graphy.core.audio_assembler.asyncio.create_subprocess_exec",
            failing_exec,
        )

        with pytest.raises(AudioAssemblyProcessError, match="could not be started"):
            await AudioAssembler(root).assemble([source], "merged.wav")

    @pytest.mark.parametrize("remove_source", (False, True))
    async def test_source_change_during_ffmpeg_blocks_publication(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        remove_source: bool,
    ) -> None:
        root = tmp_path / "audio"
        root.mkdir()
        source = _write_audio(root, "source.wav", b"original")
        target = _write_audio(root, "merged.wav", b"old-output")

        def change_source() -> None:
            if remove_source:
                source.unlink()
            else:
                source.write_bytes(b"changed-and-longer")

        _install_subprocess(
            monkeypatch,
            {source: ProbeResult()},
            on_ffmpeg=change_source,
        )

        with pytest.raises(AudioAssemblyValidationError, match="source changed"):
            await AudioAssembler(root).assemble([source], "merged.wav")

        assert target.read_bytes() == b"old-output"
