"""Bitcoin provider, backed by the free Blockstream Esplora API (no key needed).

Docs: https://github.com/Blockstream/esplora/blob/master/API.md
Bitcoin has no native "tokens" concept (BRC-20/Ordinals aside, which are out
of scope here), so `tokens` is always empty.
"""

from __future__ import annotations

import re

import requests

from .base import BaseProvider, WalletBalance

BLOCKSTREAM_API = "https://blockstream.info/api"

# Legacy (1...), P2SH (3...), bech32 (bc1q...), taproot (bc1p...)
_BTC_RE = re.compile(
    r"^(1[a-km-zA-HJ-NP-Z1-9]{25,34}|3[a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[a-z0-9]{25,90})$"
)


class BitcoinProvider(BaseProvider):
    chain_id = "bitcoin"
    display_name = "Bitcoin"
    native_symbol = "BTC"

    @classmethod
    def matches(cls, address: str) -> bool:
        return bool(_BTC_RE.match(address.strip()))

    def get_balance(self, address: str) -> WalletBalance:
        address = address.strip()
        try:
            resp = requests.get(f"{BLOCKSTREAM_API}/address/{address}", timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            return WalletBalance(
                chain=self.chain_id,
                address=address,
                native_symbol=self.native_symbol,
                error=f"API error: {exc}",
            )

        chain_stats = data.get("chain_stats", {})
        mempool_stats = data.get("mempool_stats", {})
        funded = chain_stats.get("funded_txo_sum", 0) + mempool_stats.get(
            "funded_txo_sum", 0
        )
        spent = chain_stats.get("spent_txo_sum", 0) + mempool_stats.get(
            "spent_txo_sum", 0
        )
        balance_sats = funded - spent
        balance_btc = balance_sats / 1e8

        return WalletBalance(
            chain=self.chain_id,
            address=address,
            native_symbol=self.native_symbol,
            native_amount=balance_btc,
            tokens=[],
        )
