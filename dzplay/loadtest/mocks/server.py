"""V6 phase 21: every outside service of the app, faked for staging (nothing leaves the test network).

HTTP on :9000 (one Starlette app, run by uvicorn inside the app image), SMTP on :2525.

* Telegram Bot API   — POST /bot<token>/<method> answers ok; getFile + GET /file/bot<token>/<path> serve a small pool
                       of WebP pictures (the seeded avatars and post pictures point at them). Nothing is sent anywhere.
* Bybit              — GET /v5/market/tickers and /v5/market/kline from fixtures/bybit_tickers.json (fixed data).
* TronGrid           — POST /wallet/gettransactioninfobyid (every TXID is a confirmed transfer of MOCK_AMOUNT USDT to
                       MOCK_TRON_WALLET_HEX) and /wallet/getnowblock.
* BSC JSON-RPC       — POST /bsc: eth_getTransactionReceipt / eth_blockNumber / eth_getBlockByNumber, same idea.
* SMTP sink          — keeps the last 6-digit code per recipient; GET /mail/code?to=… gives it to the sign-up scenario.
* GET /stats         — calls per service: proves e.g. that the market is not fetched once per user.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import time
from collections import Counter
from contextlib import asynccontextmanager
from email import message_from_bytes, policy
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

HERE = Path(__file__).resolve().parent
TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
USDT_TRC20_HEX = "a614f803b6fd780986a42c78ec9c7f77e6ded13c"  # the official contract, as 20-byte hex
USDT_BEP20 = "0x55d398326f99059ff775485246999027b3197955"
HEAD = 70_000_000
AMOUNT = float(os.environ.get("MOCK_AMOUNT", "50"))
TRON_WALLET_HEX = os.environ.get("MOCK_TRON_WALLET_HEX", "11" * 20).lower()
BSC_WALLET = os.environ.get("MOCK_BSC_WALLET", "0x" + "22" * 20).lower().removeprefix("0x")
LATENCY_MS = float(os.environ.get("MOCK_LATENCY_MS", "30"))  # a real service is never instant

calls: Counter = Counter()
codes: dict[str, str] = {}
_msg_id = iter(range(1, 10**12))
CODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


# ----------------------------------------------------------------------------------------- pictures


def _pictures() -> dict[str, bytes]:
    """8 avatars (256 px) + 8 post pictures (1080 px), WebP, made once at start (deterministic colours)."""
    from PIL import Image, ImageDraw

    out = {}
    for kind, side in (("avatar", 256), ("post", 1080)):
        for i in range(8):
            h = hashlib.sha256(f"{kind}{i}".encode()).digest()
            im = Image.new("RGB", (side, side if kind == "avatar" else side * 4 // 5), tuple(h[:3]))
            d = ImageDraw.Draw(im)
            for j in range(12):  # some detail so the encoder has work, like a real photo
                x = (h[j] * side) // 256
                d.rectangle([x, 0, x + side // 20, im.height], fill=tuple(h[3 + j % 20:6 + j % 20]))
            buf = io.BytesIO()
            im.save(buf, "WEBP", quality=80)
            out[f"{kind}{i}"] = buf.getvalue()
    return out


PICTURES: dict[str, bytes] = {}


def _picture_for(file_id: str) -> bytes:
    kind = "post" if "post" in file_id else "avatar"
    n = int(hashlib.sha256(file_id.encode()).hexdigest(), 16) % 8
    return PICTURES[f"{kind}{n}"]


async def _slow() -> None:
    if LATENCY_MS > 0:
        await asyncio.sleep(LATENCY_MS / 1000)


# ----------------------------------------------------------------------------------------- Telegram


async def telegram(request: Request) -> Response:
    method = request.path_params["method"]
    calls["telegram"] += 1
    calls[f"telegram:{method}"] += 1
    await _slow()
    if method == "getFile":
        try:
            body = await request.json()
        except ValueError:
            body = dict(request.query_params)
        file_id = str(body.get("file_id") or "")
        data = _picture_for(file_id)
        return JSONResponse({"ok": True, "result": {"file_id": file_id, "file_unique_id": file_id[:16],
                                                    "file_size": len(data), "file_path": f"photos/{file_id[:60]}.webp"}})
    if method == "getMe":
        return JSONResponse({"ok": True, "result": {"id": 1, "is_bot": True, "username": "staging_bot"}})
    if method == "getWebhookInfo":
        return JSONResponse({"ok": True, "result": {"url": "", "pending_update_count": 0}})
    if method == "sendDocument":
        mid = next(_msg_id)
        fid = f"lt-upload-{mid}"
        return JSONResponse({"ok": True, "result": {"message_id": mid, "document": {"file_id": fid, "file_unique_id": fid}}})
    return JSONResponse({"ok": True, "result": {"message_id": next(_msg_id), "date": int(time.time())}})


async def telegram_file(request: Request) -> Response:
    calls["telegram_file"] += 1
    await _slow()
    name = request.path_params["path"].rsplit("/", 1)[-1].removesuffix(".webp")
    return Response(_picture_for(name), media_type="image/webp")


# ----------------------------------------------------------------------------------------- Bybit


TICKERS = json.loads((HERE / "fixtures" / "bybit_tickers.json").read_text())


async def bybit_tickers(request: Request) -> Response:
    calls["bybit"] += 1
    await _slow()
    return JSONResponse({"retCode": 0, "retMsg": "OK", "result": {"category": "spot", "list": TICKERS}})


async def bybit_kline(request: Request) -> Response:
    calls["bybit"] += 1
    await _slow()
    sym = request.query_params.get("symbol", "BTCUSDT")
    base = next((float(t["lastPrice"]) for t in TICKERS if t["symbol"] == sym), 1.0)
    now = int(time.time()) // 3600 * 3600 * 1000
    rows = [[str(now - i * 3_600_000), "0", "0", "0", f"{base * (1 + ((i * 7) % 11 - 5) / 200):.6f}", "0", "0"] for i in range(24)]
    return JSONResponse({"retCode": 0, "retMsg": "OK", "result": {"category": "spot", "list": rows}})


# ----------------------------------------------------------------------------------------- chains


async def tron(request: Request) -> Response:
    calls["trongrid"] += 1
    await _slow()
    body = await request.json()
    if request.url.path.endswith("getnowblock"):
        return JSONResponse({"block_header": {"raw_data": {"number": HEAD}}})
    txid = str(body.get("value") or "")
    return JSONResponse({"id": txid, "blockNumber": HEAD - 40, "blockTimeStamp": int(time.time() * 1000) - 60_000,
                         "receipt": {"result": "SUCCESS"},
                         "log": [{"address": USDT_TRC20_HEX, "topics": [TRANSFER_TOPIC, "0" * 24 + "33" * 20, "0" * 24 + TRON_WALLET_HEX],
                                  "data": f"{round(AMOUNT * 1_000_000):064x}"}]})


async def bsc(request: Request) -> Response:
    calls["bsc"] += 1
    await _slow()
    body = await request.json()
    method = body.get("method")
    if method == "eth_blockNumber":
        result = hex(HEAD)
    elif method == "eth_getBlockByNumber":
        result = {"timestamp": hex(int(time.time()) - 60)}
    elif method == "eth_getTransactionReceipt":
        result = {"status": "0x1", "blockNumber": hex(HEAD - 40), "logs": [{
            "address": USDT_BEP20, "topics": ["0x" + TRANSFER_TOPIC, "0x" + "0" * 24 + "44" * 20, "0x" + "0" * 24 + BSC_WALLET],
            "data": hex(round(AMOUNT * 10**18))}]}
    else:
        result = None
    return JSONResponse({"jsonrpc": "2.0", "id": body.get("id", 1), "result": result})


# ----------------------------------------------------------------------------------------- SMTP sink


async def _smtp_session(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    rcpts: list[str] = []

    async def say(line: str) -> None:
        writer.write(line.encode() + b"\r\n")
        await writer.drain()

    try:
        await say("220 mock.staging ESMTP")
        while True:
            raw = await reader.readline()
            if not raw:
                break
            cmd = raw.decode(errors="replace").strip()
            verb = cmd[:4].upper()
            if verb in ("EHLO", "HELO"):
                await say("250-mock.staging\r\n250 8BITMIME")
            elif verb == "MAIL":
                rcpts = []
                await say("250 OK")
            elif verb == "RCPT":
                m = re.search(r"<([^>]+)>", cmd)
                rcpts.append((m.group(1) if m else cmd[8:]).strip().lower())
                await say("250 OK")
            elif verb == "DATA":
                await say("354 End data with <CR><LF>.<CR><LF>")
                data = await reader.readuntil(b"\r\n.\r\n")
                msg = message_from_bytes(data[:-5].replace(b"\r\n..", b"\r\n."), policy=policy.default)
                body = msg.get_body(preferencelist=("plain",))
                text = body.get_content() if body is not None else ""
                found = CODE.search(text)
                calls["smtp"] += 1
                for r in rcpts:
                    if found:
                        codes[r] = found.group(1)
                await say("250 OK queued")
            elif verb == "QUIT":
                await say("221 bye")
                break
            else:  # RSET, NOOP, …
                await say("250 OK")
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()


async def mail_code(request: Request) -> Response:
    to = (request.query_params.get("to") or "").lower()
    code = codes.pop(to, None)
    return JSONResponse({"code": code}, status_code=200 if code else 404)


async def stats(request: Request) -> Response:
    return JSONResponse(dict(calls))


@asynccontextmanager
async def lifespan(_app):
    PICTURES.update(_pictures())
    server = await asyncio.start_server(_smtp_session, "0.0.0.0", int(os.environ.get("MOCK_SMTP_PORT", "2525")))
    yield
    server.close()


app = Starlette(routes=[
    Route("/bot{token}/{method}", telegram, methods=["GET", "POST"]),
    Route("/file/bot{token}/{path:path}", telegram_file),
    Route("/v5/market/tickers", bybit_tickers),
    Route("/v5/market/kline", bybit_kline),
    Route("/wallet/gettransactioninfobyid", tron, methods=["POST"]),
    Route("/wallet/getnowblock", tron, methods=["POST"]),
    Route("/bsc", bsc, methods=["POST"]),
    Route("/mail/code", mail_code),
    Route("/stats", stats),
], lifespan=lifespan)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("MOCK_HTTP_PORT", "9000")), log_level="warning",
                access_log=False)
