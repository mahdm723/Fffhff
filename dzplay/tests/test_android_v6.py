"""V6 phase 10: the Android app — named from gradle.properties (same as APP_NAME), no calls, so no camera,
microphone, audio routing or full-screen notification; the package id and the published APK stay in step.
Phase 11c (3.1.0): the new logo — adaptive icon with a monochrome layer, a round icon, the logo on the splash.
Phase 14 (3.2.0): the app proves itself with Android Key Attestation (Android 7.0+), texts in three languages."""

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
    assert int(p["appVersionCode"]) >= 8 and p["appVersionName"] == "3.2.0"
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


def test_the_app_proves_itself_with_key_attestation():
    """V6 phase 14 (3.2.0): a hardware key bound to the server's challenge, its chain sent once, later launches sign;
    the access token becomes the WebView's dz_app cookie; «إعادة المحاولة» / an expired cookie ask for a new proof."""
    gradle = (ANDROID / "app" / "build.gradle").read_text()
    assert re.search(r"minSdk 24\b", gradle)  # setAttestationChallenge: Android 7.0
    attest = (ANDROID / "app" / "src" / "main" / "java" / "io" / "dzplay" / "app" / "AppAttest.java").read_text()
    for needle in ('"AndroidKeyStore"', "setAttestationChallenge(", "getCertificateChain(", '"/api/app/challenge"',
                   '"/api/app/attest"', '"/api/app/proof"', '"SHA256withECDSA"', '"dz_app=" + token', "HttpOnly",
                   '"X-DZ-Requested", "1"'):
        assert needle in attest, needle
    for word in ("getImei", "getDeviceId", "Secure.ANDROID_ID", "READ_PHONE_STATE", "AccountManager", "getLine1Number"):
        assert word not in attest, word  # nothing personal leaves the phone
    main = (ANDROID / "app" / "src" / "main" / "java" / "io" / "dzplay" / "app" / "MainActivity.java").read_text()
    assert "public void refreshAccess()" in main and "AppAttest.refresh(" in main
    js = (ROOT / "static" / "js" / "gate.js").read_text() + (ROOT / "static" / "js" / "api.js").read_text()
    assert js.count("refreshAccess") >= 4  # the access page's retry button and api.js on app_required


def test_android_texts_in_three_languages():
    res = ANDROID / "app" / "src" / "main" / "res"
    names = {lang: set(re.findall(r'<string name="(\w+)"', (res / folder / "strings.xml").read_text()))
             for lang, folder in (("ar", "values"), ("fr", "values-fr"), ("en", "values-en"))}
    assert names["ar"] == names["fr"] == names["en"] >= {"lang", "offline", "retry", "newMessage", "shareTitle"}
    assert '>fr<' in (res / "values-fr" / "strings.xml").read_text()
    notifications = (ANDROID / "app" / "src" / "main" / "java" / "io" / "dzplay" / "app" / "Notifications.java").read_text()
    service = (ANDROID / "app" / "src" / "fcm" / "java" / "io" / "dzplay" / "app" / "MessagingService.java").read_text()
    assert 'getData().get("title")' in service and 'getData().get("body")' in service and "clean(title" in notifications
