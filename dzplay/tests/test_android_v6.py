"""V6 phase 10: the Android app 3.0.0 — named from gradle.properties (same as APP_NAME), no calls, so no camera,
microphone, audio routing or full-screen notification; the package id and the published APK stay in step."""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent
ANDROID = ROOT / "android"


def _props() -> dict[str, str]:
    out = {}
    for line in (ANDROID / "gradle.properties").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def test_name_and_version_come_from_gradle_properties():
    p = _props()
    assert p["appName"] == Settings.model_fields["APP_NAME"].default == "DALTA.BIT"
    assert int(p["appVersionCode"]) >= 6 and p["appVersionName"] == "3.0.0"
    gradle = (ANDROID / "app" / "build.gradle").read_text()
    assert "resValue 'string', 'appName', appName" in gradle
    assert "applicationId 'io.dzplay.app'" in gradle  # never changes: updates install over the old app
    strings = (ANDROID / "app" / "src" / "main" / "res" / "values" / "strings.xml").read_text()
    assert 'name="appName"' not in strings and "DZPLAY" not in strings


def test_no_call_permissions_or_code_left():
    manifest = (ANDROID / "app" / "src" / "main" / "AndroidManifest.xml").read_text()
    perms = set(re.findall(r'uses-permission android:name="android\.permission\.(\w+)"', manifest))
    assert perms == {"INTERNET", "POST_NOTIFICATIONS"}
    assert "uses-feature" not in manifest and "DeclineReceiver" not in manifest and ".MessagingService" in manifest
    java = "\n".join(p.read_text() for p in ANDROID.rglob("*.java"))
    for word in ("AudioManager", "RECORD_AUDIO", "CAMERA", "setFullScreenIntent", "CallNotifications",
                 "takePendingAnswer", "setSpeaker", "USE_FULL_SCREEN_INTENT"):
        assert word not in java, word
    assert "request.deny()" in java  # a page asking for the camera / microphone is refused


def test_published_apk_matches_the_build_numbers():
    meta = json.loads((ROOT / "static" / "download" / "version.json").read_text(encoding="utf-8"))
    p = _props()
    assert meta["version_code"] == int(p["appVersionCode"]) and meta["version_name"] == p["appVersionName"]
    assert (ROOT / "static" / "download" / "dzplay.apk").stat().st_size > 10_000
    assert not any(w in " ".join(meta["notes"]) for w in ("مكالم", "Reels", "استوديو", "DZPLAY"))
