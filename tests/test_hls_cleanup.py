from __future__ import annotations

from pathlib import Path

import pytest

from footboy.mux.cleanup import HlsOutputCleaner


def playlist(folder: Path, names: list[str], *, duration: float = 2, init: str | None = None):
    lines = ["#EXTM3U"]
    if init:
        lines.append(f'#EXT-X-MAP:URI="{init}"')
    for name in names:
        lines.extend((f"#EXTINF:{duration},", name))
    (folder / "live.m3u8").write_text("\n".join(lines) + "\n")


@pytest.mark.parametrize("extension", ["ts", "m4s"])
def test_cleanup_preserves_playback_and_current_writer_then_retires_old_files(tmp_path, extension):
    old = f"seg_1_1.{extension}"
    retired = f"seg_1_2.{extension}"
    current = f"seg_2_1.{extension}"
    buffered = f"seg_2_2.{extension}"
    writing = f"seg_2_3.{extension}.tmp"
    abandoned = f"seg_1_3.{extension}.tmp"
    names = [old, retired, current, buffered, writing, abandoned, "init_1.mp4", "init_2.mp4"]
    for name in [*names, ".gitkeep", "personal.ts"]:
        (tmp_path / name).write_bytes(b"fixture")
    (tmp_path / f"seg_1_99.{extension}").mkdir()
    playlist(tmp_path, [old, current], init="init_1.mp4")
    cleaner = HlsOutputCleaner(tmp_path)
    cleaner.collect(2, now=0)
    cleaner.collect(2, now=61)

    assert not (tmp_path / retired).exists()
    assert not (tmp_path / abandoned).exists()
    protected = [old, current, buffered, writing, "init_1.mp4", "init_2.mp4"]
    assert all((tmp_path / name).read_bytes() == b"fixture" for name in protected)
    assert (tmp_path / ".gitkeep").exists() and (tmp_path / "personal.ts").exists()
    assert (tmp_path / f"seg_1_99.{extension}").is_dir()

    # Even an old file gets a full grace period after leaving the playlist.
    playlist(tmp_path, [current], init="init_2.mp4")
    cleaner.collect(2, now=62)
    cleaner.collect(2, now=121)
    assert (tmp_path / old).exists() and (tmp_path / "init_1.mp4").exists()
    cleaner.collect(2, now=123)
    assert not (tmp_path / old).exists()
    assert not (tmp_path / "init_1.mp4").exists()
    assert all((tmp_path / name).exists() for name in [current, buffered, writing, "init_2.mp4"])


@pytest.mark.parametrize(
    "invalid",
    [None, "not a playlist", "#EXTM3U\n#EXTINF:NaN,\n", "#EXTM3U\n#EXT-X-MAP:bad\n"],
)
def test_cleanup_defers_deletion_when_playlist_cannot_be_read(tmp_path, invalid):
    retired = tmp_path / "seg_1_1.ts"
    retired.write_bytes(b"old segment")
    playlist(tmp_path, [])
    cleaner = HlsOutputCleaner(tmp_path)
    cleaner.collect(2, now=0)
    manifest = tmp_path / "live.m3u8"
    if invalid is None:
        manifest.unlink()
    else:
        manifest.write_text(invalid)
    cleaner.collect(2, now=70)
    assert retired.exists()
    playlist(tmp_path, [])
    cleaner.collect(2, now=80)
    cleaner.collect(2, now=139)
    assert retired.exists()
    cleaner.collect(2, now=141)
    assert not retired.exists()


def test_cleanup_waits_a_full_previous_playlist_window(tmp_path):
    old = tmp_path / "seg_1_1.ts"
    old.write_bytes(b"long segment")
    playlist(tmp_path, [old.name], duration=90)
    cleaner = HlsOutputCleaner(tmp_path)
    cleaner.collect(2, now=0)
    playlist(tmp_path, ["seg_2_1.ts"])
    cleaner.collect(2, now=1)
    cleaner.collect(2, now=70)
    assert old.exists()
    cleaner.collect(2, now=92)
    assert not old.exists()


def test_stopped_session_keeps_final_playlist_but_releases_unreferenced_files(tmp_path):
    referenced, buffered = tmp_path / "seg_2_1.ts", tmp_path / "seg_2_2.ts"
    referenced.write_bytes(b"last playable segment")
    buffered.write_bytes(b"no longer referenced")
    playlist(tmp_path, [referenced.name])
    cleaner = HlsOutputCleaner(tmp_path)
    cleaner.collect(2, now=0)
    cleaner.collect(None, now=100)
    cleaner.collect(None, now=161)
    assert referenced.exists() and (tmp_path / "live.m3u8").exists()
    assert not buffered.exists()


def test_cleanup_does_not_remove_a_file_that_is_still_changing(tmp_path):
    output = tmp_path / "seg_1_1.ts.tmp"
    output.write_bytes(b"partial")
    playlist(tmp_path, [])
    cleaner = HlsOutputCleaner(tmp_path)
    cleaner.collect(None, now=0)
    output.write_bytes(b"more data arrived")
    cleaner.collect(None, now=59)
    cleaner.collect(None, now=61)
    assert output.exists()
    cleaner.collect(None, now=120)
    assert not output.exists()


def test_cleanup_never_follows_or_removes_generated_name_symlinks(tmp_path):
    outside = tmp_path / "personal.txt"
    outside.write_bytes(b"not generated")
    link = tmp_path / "seg_1_1.ts"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前环境不支持创建符号链接")
    playlist(tmp_path, [])
    cleaner = HlsOutputCleaner(tmp_path)
    cleaner.collect(None, now=0)
    cleaner.collect(None, now=61)
    assert link.is_symlink() and outside.read_bytes() == b"not generated"
