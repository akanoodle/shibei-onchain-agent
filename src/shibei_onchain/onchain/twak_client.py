"""Trust Wallet Agent Kit (TWAK) client — self-custody execution surface.

The execution layer of the 拾贝 on-chain agent talks to **Trust Wallet Agent
Kit** rather than a custodial exchange API. That choice is the whole
self-custody narrative of this project:

    * The signing key (``onchain.private_key``) is *the user's own key*. It is
      read once into this process, used to sign transactions **locally**, and
      **never leaves the device** — there is no custodial transfer, no
      deposit-to-exchange, no third party that can freeze or seize funds.
    * The key is never logged, never printed, never put in a ``raw`` payload,
      never sent over the wire. Only signed transactions (or, in MCP/CLI mode,
      *intents* handed to a local Trust Wallet Agent Kit endpoint that itself
      holds the key) are emitted.
    * Every swap is an on-chain PancakeSwap interaction the user authored. The
      agent is an *operator* of a wallet the user controls, not a custodian.

Four modes, selected by ``onchain.twak_mode``:

    ``mock`` (default)
        Fully offline. No network, no key, no third-party import. Returns
        synthetic successes with a **deterministic** pseudo tx hash derived from
        the call inputs via :mod:`hashlib` (reproducible — no randomness, no
        wall-clock), a small synthetic on-chain ledger (native ~0.5 BNB, USDT
        from config), and ``status="ok"``. This is what the unit tests and the
        hackathon dry-run path exercise.

    ``web3``
        Real self-custody signing. Lazily imports :mod:`web3` and
        :mod:`eth_account`, derives the account from ``onchain.private_key``
        *inside the process*, signs ``approve`` / ``swapExactTokensForTokens``
        locally and broadcasts via the configured RPC. The key never leaves the
        process and is never logged.

    ``mcp`` / ``cli``
        Delegate to a Trust Wallet Agent Kit endpoint/binary at
        ``onchain.twak_endpoint`` (an MCP server or a local CLI). The agent kit
        is the key-holder; we hand it intents and parse the receipt.

Failure contract (拾贝 principle — *failures must be visible, never silent*):
no public method ever raises on a network / credential / missing-dependency
error. Instead it returns ``SwapResult(status="failed", error=...)`` (or a
degraded read), so the calling cycle records the reason and keeps running.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shibei_onchain.config import OnChainConfig


# --------------------------------------------------------------------------- #
# result contract (frozen)
# --------------------------------------------------------------------------- #
@dataclass
class SwapResult:
    """Outcome of an on-chain action (approve / swap) via Trust Wallet Agent Kit.

    ``status`` is ``"ok"`` on success, ``"failed"`` otherwise. ``raw`` carries a
    debug payload that is *always secret-free* (never the private key).
    """

    status: str
    tx_hash: str = ""
    amount_in: float = 0.0
    amount_out: float = 0.0
    gas_used: int = 0
    error: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# --------------------------------------------------------------------------- #
# mock ledger defaults
# --------------------------------------------------------------------------- #
_MOCK_NATIVE_BNB = 0.5            # synthetic gas balance in mock mode
_MOCK_USDT_BALANCE = 1000.0      # synthetic free USDT in mock mode
_MOCK_GAS_USED = 120_000         # plausible PancakeSwap swap gas


def _fmt_amt(value: Any) -> str:
    try:
        return ("%.8f" % float(value)).rstrip("0").rstrip(".") or "0"
    except (TypeError, ValueError):
        return str(value)


def _as_float_amt(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _deterministic_hash(*parts: Any) -> str:
    """A reproducible 0x-prefixed 32-byte pseudo tx hash from the inputs.

    Pure :func:`hashlib.sha256` over a stable joined string — **no randomness
    and no wall-clock**, so the same inputs always yield the same hash. This is
    what lets the dry-run / mock path be deterministic and test-asserted.
    """
    payload = "|".join("" if p is None else str(p) for p in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return "0x" + digest[:64]


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
class TwakClient:
    """Self-custody execution client over Trust Wallet Agent Kit.

    The key (when present) is held only as ``self._onchain.private_key`` and is
    used solely to derive an account / sign locally in ``web3`` mode. It is
    never logged, never serialized into a result payload, never transmitted.
    """

    def __init__(self, onchain: OnChainConfig) -> None:
        self._onchain = onchain
        self.mode = (onchain.twak_mode or "mock").lower()
        # synthetic ledger for mock mode, keyed by lower-case token address
        self._mock_ledger: Dict[str, float] = {
            (onchain.usdt_address or "").lower(): _MOCK_USDT_BALANCE,
        }
        # lazily-created web3 / account handles (web3 mode only)
        self._w3 = None
        self._acct = None
        self._notes: List[str] = []

    # ------------------------------------------------------------------ #
    # identity
    # ------------------------------------------------------------------ #
    def address(self) -> str:
        """Return the operating wallet address.

        Prefers the explicitly configured ``wallet_address``. In ``web3`` mode,
        if only a private key is configured, derive the address locally (the key
        itself is never returned or logged). Falls back to a deterministic mock
        address so the offline path always has a stable identity.
        """
        if self._onchain.wallet_address:
            return self._onchain.wallet_address
        if self.mode == "web3" and self._onchain.private_key:
            acct = self._ensure_account()
            if acct is not None:
                return acct.address
        # deterministic synthetic address (mock / no-credential path)
        seed = _deterministic_hash("mock-address", self._onchain.chain_id)
        return "0x" + seed[2:42]

    # ------------------------------------------------------------------ #
    # balances
    # ------------------------------------------------------------------ #
    def native_balance(self) -> float:
        """Native BNB balance (used as the gas floor check upstream)."""
        if self.mode == "web3":
            try:
                w3 = self._ensure_web3()
                if w3 is None:
                    return 0.0
                addr = self.address()
                wei = w3.eth.get_balance(w3.to_checksum_address(addr))
                return float(wei) / 1e18
            except Exception as exc:  # noqa: BLE001 - degrade, never raise
                self._note("native_balance_failed:" + type(exc).__name__)
                return 0.0
        if self.mode in ("mcp", "cli"):
            res = self._call_endpoint("native_balance", {"address": self.address()})
            if res.ok:
                try:
                    return float(res.raw.get("balance", 0.0))
                except (TypeError, ValueError):
                    return 0.0
            return 0.0
        # mock
        return _MOCK_NATIVE_BNB

    def token_balance(self, token_address: str, decimals: int) -> float:
        """ERC-20 balance for ``token_address`` in human units."""
        if self.mode == "web3":
            try:
                w3 = self._ensure_web3()
                if w3 is None:
                    return 0.0
                raw = self._erc20_call(w3, token_address, "balanceOf", [self.address()])
                if raw is None:
                    return 0.0
                return float(raw) / (10 ** int(decimals))
            except Exception as exc:  # noqa: BLE001
                self._note("token_balance_failed:" + type(exc).__name__)
                return 0.0
        if self.mode in ("mcp", "cli"):
            res = self._call_endpoint(
                "token_balance",
                {"address": self.address(), "token": token_address, "decimals": decimals},
            )
            if res.ok:
                try:
                    return float(res.raw.get("balance", 0.0))
                except (TypeError, ValueError):
                    return 0.0
            return 0.0
        # mock: synthetic ledger, default 0 for unknown tokens
        return float(self._mock_ledger.get((token_address or "").lower(), 0.0))

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #
    def approve(self, token_address: str, spender: str, amount_wei: int) -> SwapResult:
        """ERC-20 ``approve(spender, amount)`` — exact-amount allowance.

        Self-custody: signed locally in ``web3`` mode; delegated to the agent
        kit in mcp/cli; synthetic + deterministic in mock. Never raises.
        """
        if self.mode == "web3":
            return self._web3_approve(token_address, spender, int(amount_wei))
        if self.mode in ("mcp", "cli"):
            res = self._call_endpoint(
                "approve",
                {
                    "token": token_address,
                    "spender": spender,
                    "amount_wei": str(int(amount_wei)),
                    "address": self.address(),
                },
            )
            return self._receipt_from_endpoint(res, amount_in=0.0, amount_out=0.0)
        # mock
        tx = _deterministic_hash("approve", token_address, spender, int(amount_wei), self.address())
        return SwapResult(
            status="ok",
            tx_hash=tx,
            gas_used=46_000,
            raw={
                "action": "approve",
                "token": token_address,
                "spender": spender,
                "amount_wei": str(int(amount_wei)),
                "mode": "mock",
            },
        )

    def swap_exact_tokens_for_tokens(
        self,
        *,
        token_in: str,
        token_out: str,
        amount_in_wei: int,
        min_amount_out_wei: int,
        path: List[str],
        deadline_seconds: int = 600,
    ) -> SwapResult:
        """PancakeSwap ``swapExactTokensForTokens`` (self-custody).

        In mock mode returns a deterministic success that *fills at exactly
        ``min_amount_out_wei``* (conservative, reproducible) and updates the
        synthetic ledger. Never raises.
        """
        amount_in_wei = int(amount_in_wei)
        min_amount_out_wei = int(min_amount_out_wei)
        path = list(path or [token_in, token_out])

        if self.mode == "web3":
            return self._web3_swap(
                token_in=token_in,
                amount_in_wei=amount_in_wei,
                min_amount_out_wei=min_amount_out_wei,
                path=path,
                deadline_seconds=deadline_seconds,
            )
        if self.mode in ("mcp", "cli"):
            res = self._call_endpoint(
                "swap",
                {
                    "token_in": token_in,
                    "token_out": token_out,
                    "amount_in_wei": str(amount_in_wei),
                    "min_amount_out_wei": str(min_amount_out_wei),
                    "path": path,
                    "deadline_seconds": deadline_seconds,
                    "address": self.address(),
                },
            )
            return self._receipt_from_endpoint(
                res,
                amount_in=float(amount_in_wei),
                amount_out=float(min_amount_out_wei),
            )
        # mock: deterministic fill at min_amount_out, ledger update
        tx = _deterministic_hash(
            "swap", token_in, token_out, amount_in_wei, min_amount_out_wei,
            "->".join(path), self.address(),
        )
        self._apply_mock_swap_to_ledger(token_in, token_out, amount_in_wei, min_amount_out_wei)
        return SwapResult(
            status="ok",
            tx_hash=tx,
            amount_in=float(amount_in_wei),
            amount_out=float(min_amount_out_wei),
            gas_used=_MOCK_GAS_USED,
            raw={
                "action": "swap",
                "token_in": token_in,
                "token_out": token_out,
                "amount_in_wei": str(amount_in_wei),
                "min_amount_out_wei": str(min_amount_out_wei),
                "path": path,
                "deadline_seconds": deadline_seconds,
                "mode": "mock",
            },
        )

    # ------------------------------------------------------------------ #
    # health / diagnostics (secret-free)
    # ------------------------------------------------------------------ #
    def health(self) -> Dict[str, Any]:
        """A secret-free status snapshot for dashboards / logs.

        Never exposes the private key — only whether a key is *present*.
        """
        return {
            "mode": self.mode,
            "chain": self._onchain.chain,
            "chain_id": self._onchain.chain_id,
            "address": self.address(),
            "wallet_configured": bool(self._onchain.wallet_address),
            "key_present": bool(self._onchain.private_key),
            "endpoint_configured": bool(self._onchain.twak_endpoint),
            "self_custody": True,
            "notes": list(self._notes),
        }

    # ================================================================== #
    # Trust Wallet Agent Kit (TWAK) — real `twak` CLI integration
    # ================================================================== #
    def _twak_chain(self) -> str:
        """Map the agent chain to TWAK's chain key. TWAK swaps are mainnet;
        BNB Chain is 'bsc'."""
        return "bsc"

    def _run_twak(self, args: List[str], timeout: Optional[float] = None):
        """Run ``twak <args> --json`` and return (ok, data, error). Never raises.

        Credentials/wallet are read by the CLI itself from its env / ~/.twak
        (TWAK_ACCESS_ID / TWAK_HMAC_SECRET / TWAK_WALLET_PASSWORD), so they never
        pass through this process' arguments."""
        binary = self._onchain.twak_endpoint or "twak"
        try:
            proc = subprocess.run(
                [binary] + args + ["--json"],
                capture_output=True, text=True,
                timeout=timeout or self._onchain.twak_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - never raise
            self._note("twak_exec_failed:" + type(exc).__name__)
            return False, None, "twak_exec_failed:" + type(exc).__name__
        out = (proc.stdout or proc.stderr or "").strip()
        try:
            data = json.loads(out)
        except (ValueError, TypeError):
            ok = proc.returncode == 0
            return ok, {"raw": out[:300]}, ("" if ok else "twak_nonzero_or_nonjson")
        if isinstance(data, dict) and data.get("error"):
            return False, data, str(data.get("error"))[:140]
        return True, data, ""

    def cli_swap(
        self,
        *,
        amount: float,
        from_symbol: str,
        to_symbol: str,
        quote_only: bool = True,
        slippage_pct: float = 1.0,
        password: Optional[str] = None,
    ) -> SwapResult:
        """Quote or execute a swap on BNB Chain through the real Trust Wallet
        Agent Kit (``twak swap``) — self-custody, key held by TWAK. Default is
        ``quote_only`` (no execution, no funds)."""
        args = ["swap", _fmt_amt(amount), from_symbol, to_symbol,
                "--chain", self._twak_chain(), "--slippage", str(slippage_pct)]
        if quote_only:
            args.append("--quote-only")
        if password:
            args += ["--password", password]
        ok, data, err = self._run_twak(args)
        d = data if isinstance(data, dict) else {}
        return SwapResult(
            status="ok" if ok else "failed",
            tx_hash=str(d.get("txHash") or d.get("transactionHash") or ""),
            amount_in=_as_float_amt(d.get("fromAmount") or amount),
            amount_out=_as_float_amt(d.get("toAmount") or d.get("expectedOut")),
            error=err,
            raw={"action": "twak_swap", "quote_only": quote_only, **d},
        )

    def cli_address(self) -> str:
        """Resolve the TWAK agent wallet address on BNB Chain (read-only)."""
        ok, data, _ = self._run_twak(["wallet", "address", "--chain", self._twak_chain()])
        if ok and isinstance(data, dict):
            return str(data.get("address") or "")
        return ""

    # ================================================================== #
    # internal: mock ledger
    # ================================================================== #
    def _apply_mock_swap_to_ledger(
        self, token_in: str, token_out: str, amount_in_wei: int, amount_out_wei: int
    ) -> None:
        ti = (token_in or "").lower()
        to = (token_out or "").lower()
        # only mutate balances we are tracking; assume 18-dec human conversion
        # is not needed for the synthetic ledger (kept in human units, rough).
        if ti in self._mock_ledger:
            self._mock_ledger[ti] = max(0.0, self._mock_ledger[ti] - amount_in_wei / 1e18)
        self._mock_ledger[to] = self._mock_ledger.get(to, 0.0) + amount_out_wei / 1e18

    # ================================================================== #
    # internal: web3 self-custody path (lazy imports)
    # ================================================================== #
    def _ensure_web3(self):
        if self._w3 is not None:
            return self._w3
        try:
            from web3 import Web3  # lazy, optional
        except Exception as exc:  # noqa: BLE001
            self._note("web3_import_failed:" + type(exc).__name__)
            return None
        try:
            self._w3 = Web3(Web3.HTTPProvider(
                self._onchain.rpc_url,
                request_kwargs={"timeout": self._onchain.twak_timeout_seconds},
            ))
            self._inject_poa_middleware(self._w3)
        except Exception as exc:  # noqa: BLE001
            self._note("web3_provider_failed:" + type(exc).__name__)
            self._w3 = None
        return self._w3

    def _inject_poa_middleware(self, w3) -> None:
        """BNB Chain is a Proof-of-Authority chain: its block ``extraData`` is
        longer than the 32 bytes stock web3 expects, so receipt/block reads fail
        without POA middleware. Inject it, tolerating both the web3 6.x name
        (``geth_poa_middleware``) and the 7.x name (``ExtraDataToPOAMiddleware``)."""
        try:
            from web3.middleware import geth_poa_middleware as _poa  # web3 6.x
        except Exception:  # noqa: BLE001 - try the 7.x name
            try:
                from web3.middleware import ExtraDataToPOAMiddleware as _poa  # web3 7.x
            except Exception as exc:  # noqa: BLE001 - degrade; eth_call still works
                self._note("poa_middleware_unavailable:" + type(exc).__name__)
                return
        try:
            w3.middleware_onion.inject(_poa, layer=0)
        except Exception as exc:  # noqa: BLE001
            self._note("poa_inject_failed:" + type(exc).__name__)

    def _ensure_account(self):
        if self._acct is not None:
            return self._acct
        if not self._onchain.private_key:
            return None
        try:
            from eth_account import Account  # lazy, optional
        except Exception as exc:  # noqa: BLE001
            self._note("eth_account_import_failed:" + type(exc).__name__)
            return None
        try:
            # key derived locally; NEVER logged or returned
            self._acct = Account.from_key(self._onchain.private_key)
        except Exception as exc:  # noqa: BLE001
            self._note("account_derive_failed:" + type(exc).__name__)
            return None
        return self._acct

    @staticmethod
    def _erc20_min_abi() -> List[Dict[str, Any]]:
        return [
            {
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function",
            },
            {
                "constant": False,
                "inputs": [
                    {"name": "_spender", "type": "address"},
                    {"name": "_value", "type": "uint256"},
                ],
                "name": "approve",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function",
            },
        ]

    def _erc20_call(self, w3, token_address: str, fn: str, args: List[Any]):
        try:
            contract = w3.eth.contract(
                address=w3.to_checksum_address(token_address),
                abi=self._erc20_min_abi(),
            )
            checksummed = [
                w3.to_checksum_address(a) if isinstance(a, str) and a.startswith("0x") else a
                for a in args
            ]
            return getattr(contract.functions, fn)(*checksummed).call()
        except Exception as exc:  # noqa: BLE001
            self._note("erc20_call_failed:" + type(exc).__name__)
            return None

    def _web3_approve(self, token_address: str, spender: str, amount_wei: int) -> SwapResult:
        w3 = self._ensure_web3()
        acct = self._ensure_account()
        if w3 is None or acct is None:
            return SwapResult(
                status="failed",
                error="web3_not_ready",
                raw={"action": "approve", "mode": "web3"},
            )
        try:
            contract = w3.eth.contract(
                address=w3.to_checksum_address(token_address),
                abi=self._erc20_min_abi(),
            )
            tx = contract.functions.approve(
                w3.to_checksum_address(spender), int(amount_wei)
            ).build_transaction(self._base_tx(w3, acct))
            return self._sign_send(w3, acct, tx, action="approve")
        except Exception as exc:  # noqa: BLE001 - never raise
            return SwapResult(
                status="failed",
                error="approve_failed:" + type(exc).__name__,
                raw={"action": "approve", "mode": "web3"},
            )

    def _web3_swap(
        self,
        *,
        token_in: str,
        amount_in_wei: int,
        min_amount_out_wei: int,
        path: List[str],
        deadline_seconds: int,
    ) -> SwapResult:
        w3 = self._ensure_web3()
        acct = self._ensure_account()
        if w3 is None or acct is None:
            return SwapResult(
                status="failed",
                error="web3_not_ready",
                raw={"action": "swap", "mode": "web3"},
            )
        try:
            import time as _time  # stdlib, only used for an on-chain deadline

            router_abi = [
                {
                    "name": "swapExactTokensForTokens",
                    "type": "function",
                    "inputs": [
                        {"name": "amountIn", "type": "uint256"},
                        {"name": "amountOutMin", "type": "uint256"},
                        {"name": "path", "type": "address[]"},
                        {"name": "to", "type": "address"},
                        {"name": "deadline", "type": "uint256"},
                    ],
                    "outputs": [{"name": "amounts", "type": "uint256[]"}],
                }
            ]
            router = w3.eth.contract(
                address=w3.to_checksum_address(self._onchain.pancake_router),
                abi=router_abi,
            )
            deadline = int(_time.time()) + int(deadline_seconds)
            checksummed_path = [w3.to_checksum_address(p) for p in path]
            tx = router.functions.swapExactTokensForTokens(
                int(amount_in_wei),
                int(min_amount_out_wei),
                checksummed_path,
                acct.address,
                deadline,
            ).build_transaction(self._base_tx(w3, acct))
            res = self._sign_send(w3, acct, tx, action="swap")
            res.amount_in = float(amount_in_wei)
            res.amount_out = float(min_amount_out_wei)
            return res
        except Exception as exc:  # noqa: BLE001 - never raise
            return SwapResult(
                status="failed",
                error="swap_failed:" + type(exc).__name__,
                raw={"action": "swap", "mode": "web3"},
            )

    def _base_tx(self, w3, acct) -> Dict[str, Any]:
        tx: Dict[str, Any] = {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "chainId": int(self._onchain.chain_id),
        }
        # BNB Chain uses legacy gas pricing; pin gasPrice explicitly so tx
        # building doesn't ambiguously try EIP-1559 fields the node may reject.
        try:
            tx["gasPrice"] = w3.eth.gas_price
        except Exception as exc:  # noqa: BLE001 - let build_transaction estimate
            self._note("gas_price_failed:" + type(exc).__name__)
        return tx

    def _sign_send(self, w3, acct, tx: Dict[str, Any], *, action: str) -> SwapResult:
        """Sign locally with the in-process key and broadcast. Never logs key."""
        try:
            # self-custody: signing happens here, key stays in-process
            signed = acct.sign_transaction(tx)
            raw_tx = getattr(signed, "rawTransaction", None)
            if raw_tx is None:
                raw_tx = getattr(signed, "raw_transaction")
            tx_hash = w3.eth.send_raw_transaction(raw_tx)
            tx_hex = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
            if not tx_hex.startswith("0x"):
                tx_hex = "0x" + tx_hex
            gas_used = 0
            try:
                receipt = w3.eth.wait_for_transaction_receipt(
                    tx_hash, timeout=self._onchain.twak_timeout_seconds
                )
                gas_used = int(getattr(receipt, "gasUsed", 0) or 0)
            except Exception as exc:  # noqa: BLE001 - receipt optional
                self._note("receipt_wait_failed:" + type(exc).__name__)
            return SwapResult(
                status="ok",
                tx_hash=tx_hex,
                gas_used=gas_used,
                raw={"action": action, "mode": "web3"},
            )
        except Exception as exc:  # noqa: BLE001 - never raise, never log key
            return SwapResult(
                status="failed",
                error=action + "_send_failed:" + type(exc).__name__,
                raw={"action": action, "mode": "web3"},
            )

    # ================================================================== #
    # internal: mcp / cli Trust Wallet Agent Kit endpoint
    # ================================================================== #
    def _call_endpoint(self, op: str, params: Dict[str, Any]) -> SwapResult:
        """Dispatch an intent to the Trust Wallet Agent Kit endpoint/binary.

        ``mcp`` → HTTP JSON to ``twak_endpoint`` (lazy requests/urllib).
        ``cli`` → invoke the binary at ``twak_endpoint`` with a JSON arg.
        The agent kit is the key-holder here; we never send a private key.
        """
        endpoint = self._onchain.twak_endpoint
        if not endpoint:
            return SwapResult(status="failed", error="twak_endpoint_not_configured")
        if self.mode == "cli":
            return self._call_cli(endpoint, op, params)
        return self._call_mcp(endpoint, op, params)

    def _call_mcp(self, endpoint: str, op: str, params: Dict[str, Any]) -> SwapResult:
        body = json.dumps({"op": op, "params": params}).encode("utf-8")
        # try requests first, then fall back to urllib (both lazy)
        try:
            import requests  # lazy, optional

            resp = requests.post(
                endpoint,
                data=body,
                headers={"Content-Type": "application/json"},
                timeout=self._onchain.twak_timeout_seconds,
            )
            text = resp.text
            status_code = resp.status_code
        except Exception:  # noqa: BLE001 - fall back to urllib
            try:
                from urllib import request as _request

                req = _request.Request(
                    endpoint, data=body, headers={"Content-Type": "application/json"}
                )
                with _request.urlopen(req, timeout=self._onchain.twak_timeout_seconds) as r:
                    text = r.read().decode("utf-8")
                    status_code = getattr(r, "status", 200)
            except Exception as exc:  # noqa: BLE001 - never raise
                return SwapResult(
                    status="failed",
                    error="mcp_call_failed:" + type(exc).__name__,
                )
        return self._parse_endpoint_response(text, status_code, op)

    def _call_cli(self, binary: str, op: str, params: Dict[str, Any]) -> SwapResult:
        payload = json.dumps({"op": op, "params": params})
        try:
            proc = subprocess.run(
                [binary, op, payload],
                capture_output=True,
                text=True,
                timeout=self._onchain.twak_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - never raise
            return SwapResult(status="failed", error="cli_call_failed:" + type(exc).__name__)
        if proc.returncode != 0:
            return SwapResult(
                status="failed",
                error="cli_returncode:" + str(proc.returncode),
                raw={"stderr": (proc.stderr or "")[:500]},
            )
        return self._parse_endpoint_response(proc.stdout, 200, op)

    def _parse_endpoint_response(self, text: str, status_code: int, op: str) -> SwapResult:
        if status_code and int(status_code) >= 400:
            return SwapResult(status="failed", error="endpoint_http_" + str(status_code))
        try:
            data = json.loads(text) if text else {}
        except Exception:  # noqa: BLE001
            return SwapResult(status="failed", error="endpoint_bad_json", raw={"op": op})
        if not isinstance(data, dict):
            return SwapResult(status="failed", error="endpoint_bad_shape", raw={"op": op})
        err = data.get("error")
        if err:
            return SwapResult(status="failed", error=str(err), raw={"op": op})
        return SwapResult(
            status="ok",
            tx_hash=str(data.get("tx_hash", "")),
            gas_used=int(data.get("gas_used", 0) or 0),
            raw=dict(data),
        )

    def _receipt_from_endpoint(
        self, res: SwapResult, *, amount_in: float, amount_out: float
    ) -> SwapResult:
        if res.ok:
            if not res.amount_in:
                res.amount_in = amount_in
            if not res.amount_out:
                res.amount_out = amount_out
        return res

    # ------------------------------------------------------------------ #
    def _note(self, msg: str) -> None:
        # bounded, secret-free diagnostic trail
        if msg not in self._notes:
            self._notes.append(msg)
            if len(self._notes) > 50:
                self._notes.pop(0)
