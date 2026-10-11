"""V6 phase 10: the Android app — named from gradle.properties (same as APP_NAME), no calls, so no camera,
microphone, audio routing or full-screen notification; the package id and the published APK stay in step.
Phase 11c (3.1.0): the new logo — adaptive icon with a monochrome layer, a round icon, the logo on the splash."""

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
    assert int(p["appVersionCode"]) >= 7 and p["appVersionName"] == "3.1.0"
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


def test_the_new_logo_is_the_launcher_round_and_splash_icon():
    res = ANDROID / "app" / "src" / "main" / "res"
    manifest = (ANDROID / "app" / "src" / "main" / "AndroidManifest.xml").read_text()
    assert 'android:icon="@mipmap/ic_launcher"' in manifest and 'android:roundIcon="@mipmap/ic_launcher_round"' in manifest
    for name in ("ic_launcher", "ic_launcher_round"):
        xml = (res / "mipmap-anydpi-v26" / f"{name}.xml").read_text()
        assert all(f'<{layer} android:drawable="{ref}"' in xml for layer, ref in (
            ("background", "@color/iconBackground"), ("foreground", "@mipmap/ic_launcher_foreground"),
            ("monochrome", "@mipmap/ic_launcher_monochrome")))  # Android 13 themed icons
    assert '<color name="iconBackground">#0B0B0D</color>' in (res / "values" / "colors.xml").read_text()  # the brand black
    v31 = (res / "values-v31" / "themes.xml").read_text()
    assert '<item name="android:windowSplashScreenAnimatedIcon">@mipmap/ic_launcher_foreground</item>' in v31
    assert (res / "drawable-nodpi" / "splash.png").exists() and not (res / "drawable" / "splash.png").exists()
    assert "@drawable/splash" in (res / "drawable" / "launch_background.xml").read_text()
