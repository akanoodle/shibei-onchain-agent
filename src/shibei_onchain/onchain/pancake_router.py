"""PancakeSwap V2 router adapter — pricing & path routing for on-chain swaps.

This module is the *quote* seam between the brain's USD-quoted plan and the
actual DEX. It answers two questions for every intended swap:

    1. What path of token addresses connects ``token_in`` to ``token_out``?
       (``build_path`` — direct, or hop via WBNB.)
    2. How many output tokens do we expect, and what is the slippage-protected
       ``minAmountOut`` (in wei) we hand to ``swapExactTokensForTokens``?
       (``quote`` / ``get_amounts_out`` / ``min_amount_out``.)

Two execution paths, selected automatically and *visibly*:

    * **web3** — calls the on-chain ``getAmountsOut`` view via a lazily-imported
      ``web3`` provider. Used only when a usable web3 instance is available.
    * **mock** — fully offline, deterministic. Derives ``amount_out`` from a
      caller-supplied ``ref_price`` (USD per token). This needs zero third-party
      dependencies and is the default in dry-run / hackathon-demo mode.

拾贝 principle: *failures must be visible.* If the web3 view call fails for any
reason (no provider, RPC down, bad ABI, missing dep) the quote degrades to the
mock path and stamps ``source="mock"`` plus a reason — it never raises out of a
public method.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from shibei_onchain.config import OnChainConfig


# Minimal ABI fragment for the one view we use. Kept inline so the module needs
# no external ABI file; only consumed inside the lazy web3 branch.
_GET_AMOUNTS_OUT_ABI = [
    {
        "name": "getAmountsOut",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "path", "type": "address[]"},
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
    }
]

_BPS_DENOMINATOR = 10_000


class PancakeRouter:
    """PancakeSwap V2 router quote helper.

    ``web3`` is an optional, already-constructed web3 instance (duck-typed). When
    omitted (the default), every quote runs through the deterministic mock path,
    so the whole module is importable and usable with only the standard library.
    """

    def __init__(self, onchain: OnChainConfig, web3: Optional[Any] = None) -> None:
        self.onchain = onchain
        self.web3 = web3
        self.router_address = onchain.pancake_router
        # Normalised lower-case anchors for path routing.
        self._wbnb = (onchain.wbnb_address or "").lower()
        self._usdt = (onchain.usdt_address or "").lower()

    # ------------------------------------------------------------------ #
    # path routing
    # ------------------------------------------------------------------ #
    def build_path(self, token_in: str, token_out: str) -> List[str]:
        """Build a swap path from ``token_in`` to ``token_out``.

        Direct ``[in, out]`` when either leg is itself a natural hub (WBNB or
        USDT); otherwise route ``[in, WBNB, out]`` since WBNB is the deepest
        pairing hub on PancakeSwap. Degenerate equal addresses collapse to a
        single-element path.
        """
        a = (token_in or "").strip()
        b = (token_out or "").strip()
        if a.lower() == b.lower():
            return [a]

        anchors = {self._wbnb, self._usdt}
        in_is_anchor = a.lower() in anchors
        out_is_anchor = b.lower() in anchors

        # If either side is an anchor (or there is no usable WBNB to hop
        # through), trade direct. Otherwise insert the WBNB hop.
        if in_is_anchor or out_is_anchor or not self._wbnb:
            return [a, b]
        # Avoid a useless hop if one side already *is* WBNB (covered above) —
        # here neither side is, so the WBNB intermediary is meaningful.
        return [a, self.onchain.wbnb_address, b]

    def router_addr(self) -> str:
        """Configured PancakeSwap router address."""
        return self.router_address

    # ------------------------------------------------------------------ #
    # lazy web3 (self-sufficient: build our own when in web3 mode)
    # ------------------------------------------------------------------ #
    def _ensure_web3(self):
        """Return a usable web3 instance, or ``None`` to stay on the mock path.

        Uses an explicitly injected ``web3`` if present; otherwise builds one
        from ``onchain.rpc_url`` **only when** ``twak_mode == "web3"`` — so the
        default (mock) configuration never touches the network, and the router
        works standalone (no need for the caller to thread a web3 in). Injects
        POA middleware (BNB Chain is Proof-of-Authority). Never raises."""
        if self.web3 is not None:
            return self.web3
        if (self.onchain.twak_mode or "").lower() != "web3":
            return None
        try:
            from web3 import Web3  # lazy, optional
        except Exception:  # noqa: BLE001 - no web3 -> mock
            return None
        try:
            w3 = Web3(Web3.HTTPProvider(
                self.onchain.rpc_url,
                request_kwargs={"timeout": self.onchain.twak_timeout_seconds},
            ))
            self._inject_poa(w3)
            self.web3 = w3
        except Exception:  # noqa: BLE001 - degrade to mock
            self.web3 = None
        return self.web3

    @staticmethod
    def _inject_poa(w3) -> None:
        try:
            from web3.middleware import geth_poa_middleware as _poa  # web3 6.x
        except Exception:  # noqa: BLE001
            try:
                from web3.middleware import ExtraDataToPOAMiddleware as _poa  # web3 7.x
            except Exception:  # noqa: BLE001
                return
        try:
            w3.middleware_onion.inject(_poa, layer=0)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # on-chain view (lazy web3)
    # ------------------------------------------------------------------ #
    def get_amounts_out(self, amount_in_wei: int, path: List[str]) -> List[int]:
        """Call the router's ``getAmountsOut`` view via web3.

        Returns the full amounts array (``[amount_in_wei, ..., amount_out_wei]``).
        Raises only internally; callers in this module wrap it in try/except and
        degrade to the mock path. We import ``web3`` lazily so the module loads
        without it.
        """
        if self.web3 is None:
            raise RuntimeError("no_web3_provider")

        # Lazy import: keep the module dependency-free at import time.
        try:
            from web3 import Web3  # type: ignore
        except Exception as exc:  # pragma: no cover - exercised only with web3 absent
            raise RuntimeError("web3_import_failed: %s" % exc)

        to_checksum = getattr(Web3, "to_checksum_address", None) or getattr(
            Web3, "toChecksumAddress", None
        )
        checksummed = [to_checksum(addr) if to_checksum else addr for addr in path]
        router_addr = to_checksum(self.router_address) if to_checksum else self.router_address

        contract = self.web3.eth.contract(address=router_addr, abi=_GET_AMOUNTS_OUT_ABI)
        amounts = contract.functions.getAmountsOut(int(amount_in_wei), checksummed).call()
        return [int(a) for a in amounts]

    # ------------------------------------------------------------------ #
    # slippage protection
    # ------------------------------------------------------------------ #
    def min_amount_out(self, expected_out_wei: int, slippage_bps: int) -> int:
        """Slippage-protected floor in wei.

        ``min = expected * (10000 - slippage_bps) // 10000`` using integer
        arithmetic (no float drift on wei). ``slippage_bps`` is clamped to a
        sane ``[0, 10000]`` so a bogus value can never invert the floor.
        """
        bps = int(slippage_bps)
        if bps < 0:
            bps = 0
        elif bps > _BPS_DENOMINATOR:
            bps = _BPS_DENOMINATOR
        expected = int(expected_out_wei)
        if expected <= 0:
            return 0
        return (expected * (_BPS_DENOMINATOR - bps)) // _BPS_DENOMINATOR

    # ------------------------------------------------------------------ #
    # the public quote
    # ------------------------------------------------------------------ #
    def quote(
        self,
        *,
        token_in: str,
        token_out: str,
        amount_in: float,
        decimals_in: int,
        decimals_out: int,
        ref_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Quote a swap of ``amount_in`` (human units of ``token_in``).

        Returns a dict::

            {
              "amount_out":      float,   # human units of token_out
              "min_amount_out":  int,     # WEI, slippage-protected, for the swap
              "amount_in_wei":   int,     # WEI of token_in
              "path":            List[str],
              "price":           float,   # USD ref used (token-in price)
              "source":          "web3" | "mock",
              "reason":          str,     # only present when degraded
            }

        Pricing model:

        * **web3 path** — ``getAmountsOut`` gives ``amount_out_wei`` directly.
        * **mock path** — uses ``ref_price`` (USD per non-stable token). When the
          input is the stable (USDT) we are *buying* the token, so
          ``amount_out = amount_in / ref_price``; when the input is the token we
          are *selling* it, so ``amount_out = amount_in * ref_price``.
        """
        path = self.build_path(token_in, token_out)
        amount_in_wei = self._to_wei(amount_in, decimals_in)
        slippage_bps = self.onchain.max_slippage_bps

        # --- preferred: live web3 view ------------------------------------ #
        if self._ensure_web3() is not None:
            try:
                amounts = self.get_amounts_out(amount_in_wei, path)
                if amounts and amounts[-1] > 0:
                    out_wei = int(amounts[-1])
                    amount_out = self._from_wei(out_wei, decimals_out)
                    price = self._effective_price(token_in, amount_in, amount_out)
                    return {
                        "amount_out": amount_out,
                        "min_amount_out": self.min_amount_out(out_wei, slippage_bps),
                        "amount_in_wei": amount_in_wei,
                        "path": path,
                        "price": price,
                        "source": "web3",
                    }
                degrade_reason = "web3_zero_amount_out"
            except Exception as exc:  # degrade visibly, never raise
                degrade_reason = "web3_quote_failed: %s" % type(exc).__name__
        else:
            degrade_reason = "no_web3_provider"

        # --- fallback: deterministic mock --------------------------------- #
        return self._mock_quote(
            token_in=token_in,
            amount_in=amount_in,
            amount_in_wei=amount_in_wei,
            decimals_out=decimals_out,
            ref_price=ref_price,
            path=path,
            slippage_bps=slippage_bps,
            reason=degrade_reason,
        )

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _mock_quote(
        self,
        *,
        token_in: str,
        amount_in: float,
        amount_in_wei: int,
        decimals_out: int,
        ref_price: Optional[float],
        path: List[str],
        slippage_bps: int,
        reason: str,
    ) -> Dict[str, Any]:
        price = float(ref_price) if ref_price and ref_price > 0 else 0.0
        if price <= 0 or amount_in <= 0:
            return {
                "amount_out": 0.0,
                "min_amount_out": 0,
                "amount_in_wei": amount_in_wei,
                "path": path,
                "price": price,
                "source": "mock",
                "reason": reason if price > 0 else "no_ref_price",
            }

        if self._is_stable(token_in):
            # buying token with stable: out_token = stable_in / price
            amount_out = float(amount_in) / price
        else:
            # selling token for stable (or token->token priced in USD):
            # out_stable = token_in * price
            amount_out = float(amount_in) * price

        out_wei = self._to_wei(amount_out, decimals_out)
        return {
            "amount_out": amount_out,
            "min_amount_out": self.min_amount_out(out_wei, slippage_bps),
            "amount_in_wei": amount_in_wei,
            "path": path,
            "price": price,
            "source": "mock",
            "reason": reason,
        }

    def _is_stable(self, token: str) -> bool:
        return (token or "").lower() == self._usdt

    def _effective_price(self, token_in: str, amount_in: float, amount_out: float) -> float:
        """Best-effort USD-per-token reference implied by a realised quote."""
        if amount_in <= 0 or amount_out <= 0:
            return 0.0
        if self._is_stable(token_in):
            # stable in, token out: price = stable_in / token_out
            return float(amount_in) / float(amount_out)
        # token in, stable out: price = stable_out / token_in
        return float(amount_out) / float(amount_in)

    @staticmethod
    def _to_wei(amount: float, decimals: int) -> int:
        if amount <= 0:
            return 0
        return int(round(float(amount) * (10 ** int(decimals))))

    @staticmethod
    def _from_wei(amount_wei: int, decimals: int) -> float:
        if amount_wei <= 0:
            return 0.0
        return float(amount_wei) / (10 ** int(decimals))
