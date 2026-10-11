"""V6 phase 13: read a USDT payment on the blockchain — TRON (TRC20, TronGrid) and BNB Smart Chain (BEP20, JSON-RPC).

The server alone talks to the chains (never the app). This module only READS and reports: it never accepts or
rejects anything (the owner decides, in the panel or Telegram).

For one transaction id it answers: did it succeed, how many confirmations, how much of the OFFICIAL USDT contract
reached our wallet (a token with the same name from another contract counts as fake), when it happened.

* TRC20: POST {TRONGRID_URL}/wallet/gettransactioninfobyid + /wallet/getnowblock (header TRON-PRO-API-KEY when set).
  The Transfer event log holds 20-byte hex addresses; ours are base58 (T…): converted with the 0x41 prefix.
* BEP20: eth_getTransactionReceipt + eth_blockNumber + eth_getBlockByNumber on BSC_RPC_URL (a public node, no key).
Amounts are kept in micro-USDT (10^-6), whatever the token's decimals (6 on TRON, 18 on BSC).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import httpx

from app.config import Settings

log = logging.getLogger("dzplay.chain")

TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
USDT = {
    "TRC20": {"contract": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "decimals": 6},
    "BEP20": {"contract": "0x55d398326f99059fF775485246999027B3197955", "decimals": 18},
}
SUPPORTED = tuple(USDT)
EXPLORER = {"TRC20": "https://tronscan.org/#/transaction/{txid}", "BEP20": "https://bscscan.com/tx/{txid}"}

# statuses of one transaction
OK, PENDING, NOT_FOUND, FAILED, WRONG_RECIPIENT, FAKE_TOKEN, NOT_TOKEN, BEFORE_ACCOUNT, USED, ERROR, UNSUPPORTED = (
    "ok", "pending", "not_found", "failed", "wrong_recipient", "fake_token", "not_token", "before_account", "used",
    "error", "unsupported")
WAITING = (PENDING, NOT_FOUND, ERROR)  # checked again later
HARD_FAIL = (FAILED, WRONG_RECIPIENT, FAKE_TOKEN, NOT_TOKEN, BEFORE_ACCOUNT, USED)


class ChainError(Exception):
    """The node could not be reached or answered something unexpected (checked again later)."""


@dataclass
class TxResult:
    status: str
    amount_micro: int = 0  # official USDT that reached our wallet
    confirmations: int = 0
    block: int | None = None
    time: str | None = None  # ISO UTC of the block
    to: list[str] = field(default_factory=list)  # recipients seen in USDT transfers (for "wrong recipient")
    other_tokens: list[str] = field(default_factory=list)  # contracts of other tokens sent to our wallet ("fake")
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------------------------- base58 (TRON)

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58check(payload: bytes) -> str:
    raw = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


def b58decode_check(text: str) -> bytes:
    n = 0
    for ch in text:
        i = _B58.find(ch)
        if i < 0:
            raise ValueError("not base58")
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    raw = b"\0" * (len(text) - len(text.lstrip("1"))) + raw
    payload, check = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != check:
        raise ValueError("bad checksum")
    return payload


def tron_from_hex20(hex20: str) -> str:
    """20-byte hex (no prefix) → T… address."""
    return b58check(bytes.fromhex("41" + hex20[-40:]))


def tron_to_hex20(address: str) -> str:
    payload = b58decode_check(address)
    if len(payload) != 21 or payload[0] != 0x41:
        raise ValueError("not a TRON address")
    return payload[1:].hex()


def _topic_address(topic: str) -> str:
    """The 20-byte address at the end of a 32-byte log topic (hex, lower case, no 0x)."""
    t = topic[2:] if topic.startswith("0x") else topic
    return t[-40:].lower()


def _to_micro(raw: int, decimals: int) -> int:
    return raw // (10 ** (decimals - 6)) if decimals >= 6 else raw * (10 ** (6 - decimals))


def _iso(ts_seconds: float) -> str:
    return datetime.fromtimestamp(ts_seconds, tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


# ---------------------------------------------------------------------------------------------- the reader


class ChainReader:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._http = httpx.Client(timeout=httpx.Timeout(float(settings.CHAIN_TIMEOUT), connect=5.0), transport=transport,
                                  follow_redirects=False, trust_env=True)

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ TRON
    def _tron(self, path: str, body: dict) -> dict:
        headers = {"Accept": "application/json"}
        if self.settings.TRONGRID_API_KEY:
            headers["TRON-PRO-API-KEY"] = self.settings.TRONGRID_API_KEY
        try:
            r = self._http.post(self.settings.TRONGRID_URL.rstrip("/") + path, json=body, headers=headers)
            if r.status_code != 200:
                raise ChainError(f"TronGrid {path}: HTTP {r.status_code}")
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ChainError(f"TronGrid {path}: {type(exc).__name__}") from None
        if not isinstance(data, dict):
            raise ChainError(f"TronGrid {path}: unexpected answer")
        return data

    def tron(self, txid: str, wallet: str) -> TxResult:
        info = self._tron("/wallet/gettransactioninfobyid", {"value": txid.lower()})
        if not info or not info.get("id"):
            return TxResult(NOT_FOUND, detail="not on the chain yet")
        block = int(info.get("blockNumber") or 0)
        when = _iso(int(info.get("blockTimeStamp") or 0) / 1000) if info.get("blockTimeStamp") else None
        now = self._tron("/wallet/getnowblock", {})
        head = int(((now.get("block_header") or {}).get("raw_data") or {}).get("number") or 0)
        confirmations = max(0, head - block + 1) if block and head else 0
        receipt = info.get("receipt") or {}
        if info.get("result") == "FAILED" or (receipt.get("result") and receipt.get("result") != "SUCCESS"):
            return TxResult(FAILED, block=block, time=when, confirmations=confirmations,
                            detail=str(receipt.get("result") or info.get("result")))
        try:
            ours = tron_to_hex20(wallet).lower()
        except ValueError:
            return TxResult(ERROR, detail="the platform wallet is not a TRON address")
        official = tron_to_hex20(USDT["TRC20"]["contract"]).lower()
        result = TxResult(PENDING, block=block, time=when, confirmations=confirmations)
        transfers = 0
        for entry in info.get("log") or []:
            topics = [str(t) for t in entry.get("topics") or []]
            if len(topics) < 3 or topics[0].lower().removeprefix("0x") != TRANSFER_TOPIC:
                continue
            contract = str(entry.get("address") or "")[-40:].lower()
            to = _topic_address(topics[2])
            if contract != official:
                if to == ours:
                    result.other_tokens.append(tron_from_hex20(contract))
                continue
            transfers += 1
            result.to.append(tron_from_hex20(to))
            if to == ours:
                result.amount_micro += _to_micro(int(str(entry.get("data") or "0"), 16), USDT["TRC20"]["decimals"])
        return self._classify(result, transfers, "TRC20")

    # ------------------------------------------------------------------ BSC
    def _rpc(self, method: str, params: list) -> object:
        try:
            r = self._http.post(self.settings.BSC_RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if r.status_code != 200:
                raise ChainError(f"BSC {method}: HTTP {r.status_code}")
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ChainError(f"BSC {method}: {type(exc).__name__}") from None
        if not isinstance(data, dict) or data.get("error"):
            raise ChainError(f"BSC {method}: {(data or {}).get('error') if isinstance(data, dict) else 'bad answer'}")
        return data.get("result")

    def bsc(self, txid: str, wallet: str) -> TxResult:
        receipt = self._rpc("eth_getTransactionReceipt", [txid.lower()])
        if not receipt:
            return TxResult(NOT_FOUND, detail="not on the chain yet")
        block = int(str(receipt.get("blockNumber") or "0x0"), 16)
        head = int(str(self._rpc("eth_blockNumber", []) or "0x0"), 16)
        confirmations = max(0, head - block + 1) if block and head else 0
        header = self._rpc("eth_getBlockByNumber", [hex(block), False]) if block else None
        when = _iso(int(str(header.get("timestamp")), 16)) if isinstance(header, dict) and header.get("timestamp") else None
        if str(receipt.get("status")) != "0x1":
            return TxResult(FAILED, block=block, time=when, confirmations=confirmations, detail="status 0x0")
        ours = wallet.lower().removeprefix("0x")
        official = USDT["BEP20"]["contract"].lower().removeprefix("0x")
        result = TxResult(PENDING, block=block, time=when, confirmations=confirmations)
        transfers = 0
        for entry in receipt.get("logs") or []:
            topics = [str(t) for t in entry.get("topics") or []]
            if len(topics) < 3 or topics[0].lower().removeprefix("0x") != TRANSFER_TOPIC:
                continue
            contract = str(entry.get("address") or "").lower().removeprefix("0x")
            to = _topic_address(topics[2])
            if contract != official:
                if to == ours:
                    result.other_tokens.append("0x" + contract)
                continue
            transfers += 1
            result.to.append("0x" + to)
            if to == ours:
                result.amount_micro += _to_micro(int(str(entry.get("data") or "0x0"), 16), USDT["BEP20"]["decimals"])
        return self._classify(result, transfers, "BEP20")

    # ------------------------------------------------------------------ common
    def _classify(self, r: TxResult, transfers: int, network: str) -> TxResult:
        if r.amount_micro > 0:
            need = self.settings_confirmations(network)
            r.status = OK if r.confirmations >= need else PENDING
            if r.status == PENDING:
                r.detail = f"{r.confirmations}/{need} confirmations"
        elif r.other_tokens:
            r.status = FAKE_TOKEN
        elif transfers:
            r.status = WRONG_RECIPIENT
        else:
            r.status = NOT_TOKEN
        return r

    def settings_confirmations(self, network: str) -> int:
        return int(getattr(self.settings, f"CHAIN_CONFIRMATIONS_{network}"))  # a panel setting (live)

    def check(self, network: str, txid: str, wallet: str) -> TxResult:
        """Never raises: a node problem is the ERROR status (checked again later)."""
        if network not in SUPPORTED:
            return TxResult(UNSUPPORTED, detail=network)
        try:
            return self.tron(txid, wallet) if network == "TRC20" else self.bsc(txid, wallet)
        except ChainError as exc:
            log.info("chain check: %s", exc)
            return TxResult(ERROR, detail=str(exc)[:200])
        except Exception as exc:  # noqa: BLE001 - a malformed answer must not break the loop
            log.warning("chain check failed: %s", type(exc).__name__)
            return TxResult(ERROR, detail=type(exc).__name__)
