from __future__ import annotations

import base64
import time

import pytest

from muse2api.core.media import load_media_refs, sniff_mime
from muse2api.drivers.base import InputImage, MediaResult, VideoRequest
from muse2api.drivers.browser.driver import BrowserDriver
from muse2api.errors import InvalidRequest

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
MP4 = b"\x00\x00\x00\x18ftypisom" + b"0" * 64
MP3 = b"ID3\x04" + b"0" * 64
WAV = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"0" * 64


def b64(data: bytes, mime: str = "application/octet-stream") -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def test_sniff_mime():
    assert sniff_mime(PNG) == "image/png"
    assert sniff_mime(MP4) == "video/mp4"
    assert sniff_mime(b"\x00\x00\x00\x18ftypM4A " + b"0" * 8) == "audio/mp4"
    assert sniff_mime(MP3) == "audio/mpeg"
    assert sniff_mime(WAV) == "audio/wav"
    assert sniff_mime(b"hello") is None


async def test_load_media_refs_types_from_header():
    out = await load_media_refs([b64(MP4), b64(MP3, "audio/mp3"), b64(PNG, "image/jpeg")])
    assert [m for _, m in out] == ["video/mp4", "audio/mpeg", "image/png"]


async def test_load_media_refs_limits():
    with pytest.raises(InvalidRequest, match="at most 10"):
        await load_media_refs([b64(PNG)] * 11)
    with pytest.raises(InvalidRequest, match="unsupported"):
        await load_media_refs([b64(b"%PDF-1.7 hello", "application/pdf")])


def test_video_prompt_text_only_forbids_earlier_files():
    p = BrowserDriver._video_prompt("waves", "9:16", 5, [])
    assert "brand-new video from this text alone" in p and "earlier" in p
    assert "vertical 9:16" in p and "exactly 5 seconds" in p and p.endswith(": waves")


def test_video_prompt_single_image_is_first_frame():
    p = BrowserDriver._video_prompt("she smiles", None, None, ["image"])
    assert "first frame" in p and "[image1]" not in p


def test_video_prompt_several_images_get_labels():
    p = BrowserDriver._video_prompt("[image2] walks in", "16:9", 8, ["image", "image"], "1080p")
    assert "[image1] is attachment 1 (image); [image2] is attachment 2 (image)" in p
    assert "first image is the opening frame" in p
    assert "16:9 widescreen" in p and "resolution 1080p" in p


def test_video_prompt_with_audio_does_not_force_first_frame():
    p = BrowserDriver._video_prompt("she sings along to [audio1]", "9:16", 6, ["image", "audio"])
    assert "[image1] is attachment 1 (image); [audio1] is attachment 2 (audio)" in p
    assert "opening frame" not in p and "first frame" not in p
    assert "attached to this message only" in p


def test_video_request_uploads_order():
    first, ref = InputImage(PNG), InputImage(MP3, "audio/mpeg")
    req = VideoRequest(prompt="x", model="m", first_frame=first, references=[ref])
    assert req.uploads == [first, ref] and ref.kind == "audio"


async def test_uploaded_reference_video_is_never_returned(tmp_path, monkeypatch):
    from muse2api.accounts import Account
    from muse2api.config import Settings

    drv = BrowserDriver(Settings(_env_file=None, data_dir=tmp_path))
    seen_ignore = []

    class Tab:
        has_state = False

    async def checkout(account, prefer=None):
        return Tab()

    async def noop(*a, **k):
        return {}

    atts = iter([{"src": "blob:upload"}, {"src": "blob:new"}])

    async def wait_media(tab, base, kind, timeout, on_progress, cancel, ignore=None):
        seen_ignore.append(set(ignore))
        return next(atts)

    async def download(tab, att, kind):
        data = MP4 if att["src"] == "blob:upload" else b"generated"
        return MediaResult(data=data, mime="video/mp4", kind="video")

    monkeypatch.setattr(drv, "_checkout", checkout)
    monkeypatch.setattr(drv, "_checkin", noop)
    monkeypatch.setattr(drv, "_prepare", noop)
    monkeypatch.setattr(drv, "_wait_media", wait_media)
    monkeypatch.setattr(drv, "_download", download)
    req = VideoRequest(prompt="match [video1]", model="m", references=[InputImage(MP4, "video/mp4")])
    result = await drv.generate_video(Account(id="a"), req)
    assert result.data == b"generated"
    assert seen_ignore == [set(), {"blob:upload"}]


def test_api_accepts_mixed_references(client, auth):
    r = client.post("/v1/videos", headers=auth, json={
        "prompt": "[image1] sings to [audio1]", "duration": 5, "size": "9:16",
        "image": b64(PNG, "image/png"), "audios": [b64(MP3)], "resolution": "1080x1920"})
    assert r.status_code == 200, r.text
    task_id = r.json()["id"]
    for _ in range(50):
        task = client.get(f"/v1/videos/{task_id}", headers=auth).json()
        if task["status"] == "succeeded":
            break
        time.sleep(0.05)
    assert task["status"] == "succeeded"


def test_api_rejects_unsupported_reference(client, auth):
    r = client.post("/v1/videos", headers=auth, json={
        "prompt": "x", "videos": [b64(b"not a video at all", "text/plain")]})
    assert r.status_code == 400
