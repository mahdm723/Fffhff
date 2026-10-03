"""V4 calls end-to-end: two phone-sized Chromium browsers (fake camera + microphone) call
each other through a real coturn rendered from deploy/coturn/turnserver.conf.template.

Checks: relay-only ICE (no host / srflx candidate is ever gathered), DTLS-SRTP up, remote
video and audio flowing, mute / camera off / audio↔video, measured bitrate (bandwidth
estimate), quality heartbeat stored server-side (metadata only), ICE restart after the TURN
server is restarted, decline, missed (ring timeout), and the call summary in the chat.

    python e2e/run_calls_e2e.py [--shots DIR]     (needs `turnserver` on PATH)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_e2e import MOBILE, Run, register  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TURN_SECRET = "e2e-turn-secret-" + os.urandom(6).hex()
TURN_PORT = 3479


def start_turn(tmp: str) -> subprocess.Popen:
    """coturn with the production template (+ loopback allowed: both browsers run on this host)."""
    tpl = (ROOT / "deploy" / "coturn" / "turnserver.conf.template").read_text()
    conf = (tpl.replace("__REALM__", "localhost").replace("__TURN_PORT__", str(TURN_PORT))
            .replace("__TURN_MIN_PORT__", "49160").replace("__TURN_MAX_PORT__", "49200"))
    conf += (f"\nstatic-auth-secret={TURN_SECRET}\nexternal-ip=127.0.0.1\nlistening-ip=127.0.0.1\n"
             "relay-ip=127.0.0.1\nallowed-peer-ip=127.0.0.1\nno-tls\nno-dtls\n")
    path = Path(tmp) / "turnserver.conf"
    path.write_text(conf)
    proc = subprocess.Popen(["turnserver", "-c", str(path), "--allow-loopback-peers"],
                            stdout=open(Path(tmp) / "turn.log", "a"), stderr=subprocess.STDOUT)
    time.sleep(1.0)
    assert proc.poll() is None, (Path(tmp) / "turn.log").read_text()[-2000:]
    return proc


def start_server(port: int, tmp: str) -> tuple[subprocess.Popen, str, str]:
    db = f"{tmp}/e2e.db"
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{db}", SECRET_KEY="e2e-secret", ENV="development",
               CLEANUP_INTERVAL="60", LOG_LEVEL="WARNING", REDIS_URL="", GOOGLE_CLIENT_ID="", MAX_ACCOUNTS_PER_IP="10",
               PUBLIC_URL=f"http://127.0.0.1:{port}", TURN_SECRET=TURN_SECRET, TURN_HOST="127.0.0.1",
               TURN_PORT=str(TURN_PORT), TURN_TLS_PORT="0", CALL_RING_TIMEOUT="10", CALL_TICK_SECONDS="1",
               CALL_RECONNECT_TIMEOUT="25", CALL_QUALITY_REPORT_SECONDS="4")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
                             "--no-access-log", "--timeout-graceful-shutdown", "2"], cwd=ROOT, env=env)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1)
            return proc, base, db
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("server did not start")


def dbg(page):
    return page.evaluate("window.dzCalls && window.dzCalls.debug()")


def wait_for(page, pred, timeout=30, what="condition"):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = dbg(page)
        if last and pred(last):
            return last
        time.sleep(0.5)
    raise AssertionError(f"timeout waiting for {what}: {json.dumps(last, ensure_ascii=False)[:600]}")


def accept_permission_sheet(page):
    sheet = page.locator(".sheet", has_text="السماح بالمكالمات")
    if sheet.count():
        sheet.get_by_role("button", name="متابعة").click()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--shots", default=str(ROOT / "e2e" / "screenshots-calls"))
    args = parser.parse_args()
    if not shutil.which("turnserver"):
        print("turnserver (coturn) not found: install it to run this test")
        return 2
    tmp = tempfile.mkdtemp(prefix="dz-e2e-calls-")
    turn = start_turn(tmp)
    proc, base, dbfile = start_server(args.port, tmp)
    run = Run(base, Path(args.shots))
    chromium = os.environ.get("PW_CHROMIUM", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    suffix = str(int(time.time()))
    report: dict = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=chromium if os.path.exists(chromium) else None,
                args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
            ctx_a = browser.new_context(**MOBILE, color_scheme="dark", locale="ar")
            ctx_b = browser.new_context(**MOBILE, color_scheme="dark", locale="ar")
            for ctx in (ctx_a, ctx_b):
                ctx.grant_permissions(["camera", "microphone"], origin=base)
            a, b = ctx_a.new_page(), ctx_b.new_page()
            run.watch(a, "A")
            run.watch(b, "B")

            print("Setup: two people in a chat where both have written")
            register(run, a, f"calla{suffix}@example.com", gender="رجل")
            register(run, b, f"callb{suffix}@example.com", gender="أنثى")
            a.goto(base + "/#/messages")
            a.locator(".msg-dock textarea").fill("مرحبا، هل نتصل؟")
            a.locator(".msg-dock .send-btn").click()
            b.goto(base + "/#/messages")
            b.locator(".conv-item").first.click(timeout=15000)
            expect(b.get_by_role("button", name="مكالمة صوتية")).to_be_enabled()  # A wrote: B may call
            b.locator(".chat__composer textarea").fill("أهلًا، نعم")
            b.get_by_role("button", name="إرسال").click()
            a.goto(base + "/#/messages")
            a.locator(".conv-item").first.click()
            expect(a.get_by_role("button", name="مكالمة فيديو")).to_be_enabled(timeout=10000)
            run.step("Call buttons enabled once the other side replied")

            print("Video call A → B")
            a.get_by_role("button", name="مكالمة فيديو").click()
            accept_permission_sheet(a)
            expect(a.locator(".call-screen .call-status")).to_contain_text("يرن", timeout=15000)
            incoming = b.locator(".call-screen.is-incoming")
            expect(incoming).to_be_visible(timeout=15000)
            expect(incoming.locator(".call-status")).to_contain_text("مكالمة فيديو واردة")
            expect(incoming.locator(".call-name")).to_contain_text("dzplay")  # anonymous chat: no name, no number
            run.shot(b, "c01-incoming")
            run.shot(a, "c02-outgoing")
            incoming.get_by_role("button", name="رد", exact=True).click()
            accept_permission_sheet(b)
            da = wait_for(a, lambda d: d["mediaUp"] and d["stats"] and d["stats"].get("dtls_state") == "connected", 40, "A media")
            db_ = wait_for(b, lambda d: d["mediaUp"] and d["stats"] and d["stats"].get("dtls_state") == "connected", 40, "B media")
            for who, d in (("A", da), ("B", db_)):
                s = d["stats"]
                assert d["policy"] == "relay", (who, d)
                assert s["local_type"] == "relay" and s["remote_type"] == "relay", (who, s)
                assert set(s["local_candidate_types"]) == {"relay"}, (who, s["local_candidate_types"])  # no host / srflx ever
                assert s["srtp_cipher"], (who, s)
            run.step(f"Connected through TURN only (relay↔relay), DTLS-SRTP {da['stats']['srtp_cipher']}")
            a.wait_for_function("document.querySelector('.call-remote').videoWidth > 0", timeout=20000)
            b.wait_for_function("document.querySelector('.call-remote').videoWidth > 0", timeout=20000)
            expect(a.locator(".call-screen.has-remote-video")).to_be_visible(timeout=10000)
            run.step("Remote video playing on both phones")

            ramp = []
            for _ in range(15):  # bandwidth estimate ramp-up over 30 s
                st = dbg(a)["stats"]
                ramp.append((st.get("kbps_out"), st.get("available_kbps"), st.get("height")))
                time.sleep(2)
            report["ramp_kbps_avail_height"] = ramp
            samples = []
            for _ in range(4):
                samples.append(dbg(a)["stats"])
                time.sleep(2)
            kbps = [s["kbps_out"] for s in samples if s.get("kbps_out")]
            report["video_kbps_out"] = round(sum(kbps) / len(kbps)) if kbps else None
            report["video_height"] = samples[-1].get("height")
            report["video_level"] = dbg(a)["level"]
            report["limited_by"] = samples[-1].get("limited_by")
            report["available_kbps"] = samples[-1].get("available_kbps")
            report["fps"] = samples[-1].get("fps")
            report["capture"] = dbg(a)["capture"]
            report["video_codec"] = samples[-1].get("codec_video")
            report["audio_codec"] = samples[-1].get("codec_audio")
            report["rtt_ms"] = samples[-1].get("rtt_ms")
            run.shot(a, "c03-in-call-video")
            assert "null" not in a.locator(".call-controls").inner_text()
            box = a.locator(".call-controls").bounding_box()
            assert box and box["height"] < 100, box  # one row of controls on a phone
            run.step(f"Video call: ~{report['video_kbps_out']} kbit/s up, {report['video_height']}p, {report['video_codec']} + {report['audio_codec']}")

            print("Controls")
            a.get_by_role("button", name="كتم الميكروفون").click()
            assert dbg(a)["muted"] is True
            a.get_by_role("button", name="إلغاء كتم الميكروفون").click()
            a.get_by_role("button", name="إيقاف الكاميرا").click()
            wait_for(b, lambda d: d["remoteVideo"] is False, 10, "B sees A's camera off")
            run.shot(b, "c04-peer-camera-off")
            a.get_by_role("button", name="تشغيل الكاميرا").click()
            wait_for(b, lambda d: d["remoteVideo"] is True, 15, "B sees A's camera back")
            run.step("Mute, camera off/on (audio↔video) reach the other side")

            print("Quality heartbeat stored server-side (metadata only)")
            time.sleep(5)
            with sqlite3.connect(dbfile) as conn:
                q = conn.execute("SELECT quality, state FROM calls ORDER BY created_at DESC LIMIT 1").fetchone()
            assert q and q[1] == "connected" and q[0] and '"relay":true' in q[0], q
            run.step("Server keeps aggregated quality (RTT / loss / bitrate / relay) — no media")

            print("Network drop: TURN server restarted mid-call → ICE restart")
            turn.send_signal(signal.SIGKILL)
            turn.wait()
            wait_for(a, lambda d: d["ice"] in ("disconnected", "failed"), 30, "A notices the drop")
            expect(a.locator(".call-banner")).to_contain_text("جارٍ إعادة الاتصال", timeout=10000)
            run.shot(a, "c05-reconnecting")
            turn = start_turn(tmp)
            wait_for(a, lambda d: d["ice"] in ("connected", "completed"), 40, "A reconnected")
            wait_for(b, lambda d: d["ice"] in ("connected", "completed"), 40, "B reconnected")
            expect(a.locator(".call-banner")).to_be_hidden(timeout=10000)
            run.step("Call recovered by ICE restart after the relay came back")

            a.get_by_role("button", name="إنهاء المكالمة").click()
            expect(b.locator(".call-screen .call-status")).to_contain_text("انتهت المكالمة", timeout=10000)
            run.shot(b, "c06-ended")
            expect(b.locator(".call-screen")).to_have_count(0, timeout=10000)
            expect(b.locator(".sys-msg--call")).to_contain_text("مكالمة فيديو", timeout=10000)
            run.shot(b, "c07-chat-summary")
            run.step("Hang up: both sides closed; the chat shows the call summary with its duration")

            print("Audio call bandwidth")
            b.get_by_role("button", name="مكالمة صوتية").click()
            accept_permission_sheet(b)
            expect(a.locator(".call-screen.is-incoming")).to_be_visible(timeout=15000)
            a.locator(".call-screen").get_by_role("button", name="رد", exact=True).click()
            accept_permission_sheet(a)
            wait_for(b, lambda d: d["mediaUp"], 40, "audio call up")
            run.shot(a, "c08-audio-call")
            time.sleep(8)
            ks = []
            for _ in range(4):
                ks.append(dbg(b)["stats"].get("kbps_out"))
                time.sleep(2)
            ks = [k for k in ks if k]
            report["audio_kbps_out"] = round(sum(ks) / len(ks)) if ks else None
            b.get_by_role("button", name="إنهاء المكالمة").click()
            expect(a.locator(".call-screen")).to_have_count(0, timeout=10000)
            run.step(f"Audio call (Opus FEC+DTX): ~{report['audio_kbps_out']} kbit/s up")

            print("Decline and missed")
            time.sleep(1)
            a.get_by_role("button", name="مكالمة صوتية").click()
            expect(b.locator(".call-screen.is-incoming")).to_be_visible(timeout=15000)
            b.locator(".call-screen").get_by_role("button", name="رفض").click()
            expect(a.locator(".call-screen .call-status")).to_contain_text("رُفضت المكالمة", timeout=10000)
            expect(a.locator(".call-screen")).to_have_count(0, timeout=10000)
            run.step("Declined: the caller is told")
            a.get_by_role("button", name="مكالمة صوتية").click()
            expect(b.locator(".call-screen.is-incoming")).to_be_visible(timeout=15000)
            expect(a.locator(".call-screen .call-status")).to_contain_text("لم يرد", timeout=25000)
            expect(b.locator(".call-screen")).to_have_count(0, timeout=15000)
            run.step("Unanswered: missed after the ring timeout on both sides")

            for page, who in ((a, "A"), (b, "B")):
                assert "@example.com" not in page.content(), f"{who} page leaks an e-mail"
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if turn.poll() is None:
            turn.terminate()

    (Path(args.shots) / "calls-report.json").write_text(json.dumps(report, indent=2))
    print("measurements:", json.dumps(report))
    noise = ("favicon", "Failed to load resource")
    errors = [e for e in run.errors if not any(n in e for n in noise)]
    if errors:
        print("\nBrowser errors:\n  " + "\n  ".join(errors))
        return 1
    print(f"\nCALLS E2E PASSED. Screenshots in {run.shots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
