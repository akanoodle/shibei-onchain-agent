"""Shibei On-Chain Agent — an autonomous BNB Chain trading agent.

Three sponsor capabilities, three layers:

    Signal     · CoinMarketCap Agent Hub  (regime + risk flags, hard BTC gate)
    Decision   · 拾贝 V3.0 brain           (ranking, sizing, full risk stack)
    Execution  · Trust Wallet Agent Kit    (self-custody PancakeSwap on BNB Chain)

Importing this package has no side effects and pulls in no third-party deps;
heavy / optional dependencies (web3, requests) are imported lazily inside the
modules that need them so the package always imports under a bare stdlib env.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
