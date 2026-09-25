"""Ethereum provider.

Native ETH balance uses a free public JSON-RPC endpoint (no key required).

ERC-20 token balances require an indexer since raw RPC has no "list tokens
held by address" call. If the ETHERSCAN_API_KEY environment variable is set,
this provider uses Etherscan's free `tokentx` endpoint to discover which
ERC-20 contracts the address has ever interacted with, then reads the
*current* balance of each contract directly on-chain via `balanceOf` (so the
amount is always live, not just "ever received"). Without an API key, native
balance still works but tokens are skipped with a warning.

Get a free key at https://etherscan.io/apis

Staked ETH: liquid-staking derivatives (stETH, rETH, cbETH, ...) are plain
ERC-20 tokens sitting in the wallet, so they're already covered by the
token discovery above (once ETHERSCAN_API_KEY is set). What's invisible to
a normal balance/token check is *native* beacon-chain staking: the 32 ETH
per validator moved into the deposit contract, tracked only on the beacon
chain, not the execution-layer account. This provider looks those up via
beaconcha.in's free public API (https://beaconcha.in/api/v1/docs/), which
maps an execution-layer address (as depositor or 0x01 withdrawal
credential) to its beacon validators and their current balances. An
optional BEACONCHAIN_API_KEY raises the (generous) default rate limit.
"""

from __future__ import annotations

import os
import re

import requests

from .base import BaseProvider, TokenBalance, WalletBalance

# Several free public RPC endpoints, tried in order. Public RPCs are
# rate-limited and occasionally flaky, so a single endpoint isn't reliable
# enough on its own. If ETH_RPC_URL is set, it's tried first.
_DEFAULT_RPCS = [
    "https://eth.llamarpc.com",
    "https://ethereum-rpc.publicnode.com",
    "https://rpc.ankr.com/eth",
    "https://cloudflare-eth.com",
]
def _public_rpcs() -> list[str]:
    # Read lazily (not at import time) so a caller (e.g. a GUI app) can
    # change these env vars at runtime and have it take effect immediately.
    env_rpc = os.environ.get("ETH_RPC_URL")
    return [env_rpc] + _DEFAULT_RPCS if env_rpc else _DEFAULT_RPCS


ETHERSCAN_API = "https://api.etherscan.io/api"


def _etherscan_key() -> str:
    return os.environ.get("ETHERSCAN_API_KEY", "")


BEACONCHAIN_API = "https://beaconcha.in/api/v1"


def _beacon_key() -> str:
    return os.environ.get("BEACONCHAIN_API_KEY", "")

_ETH_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

# ERC-20 balanceOf(address) selector
_BALANCE_OF_SELECTOR = "0x70a08231"


class EthereumProvider(BaseProvider):
    chain_id = "ethereum"
    display_name = "Ethereum"
    native_symbol = "ETH"

    @classmethod
    def matches(cls, address: str) -> bool:
        return bool(_ETH_RE.match(address.strip()))

    def _rpc_call(self, method: str, params: list) -> dict:
        """Try each configured RPC endpoint until one returns a valid result."""
        last_error: Exception | None = None
        for endpoint in _public_rpcs():
            payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            try:
                resp = requests.post(endpoint, json=payload, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                continue
            if "error" in data:
                last_error = RuntimeError(
                    f"{endpoint} returned RPC error: {data['error']}"
                )
                continue
            if "result" not in data:
                last_error = RuntimeError(
                    f"{endpoint} returned unexpected response: {data}"
                )
                continue
            return data
        raise RuntimeError(
            f"All RPC endpoints failed for {method}. Last error: {last_error}"
        )

    def _get_native_balance(self, address: str) -> float:
        result = self._rpc_call("eth_getBalance", [address, "latest"])
        wei = int(result["result"], 16)
        return wei / 1e18

    def _discover_token_contracts(self, address: str) -> dict[str, dict]:
        """Return {contract_address: {"symbol":..., "name":..., "decimals":...}}"""
        params = {
            "module": "account",
            "action": "tokentx",
            "address": address,
            "sort": "desc",
            "apikey": _etherscan_key(),
        }
        resp = requests.get(ETHERSCAN_API, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        contracts: dict[str, dict] = {}
        for tx in data.get("result", []) or []:
            if not isinstance(tx, dict):
                continue
            contract = tx.get("contractAddress")
            if not contract or contract in contracts:
                continue
            try:
                decimals = int(tx.get("tokenDecimal", "18"))
            except ValueError:
                decimals = 18
            contracts[contract] = {
                "symbol": tx.get("tokenSymbol", "?"),
                "name": tx.get("tokenName", "Unknown token"),
                "decimals": decimals,
            }
        return contracts

    def _get_token_balance(self, address: str, contract: str, decimals: int) -> float:
        padded_addr = address.lower().replace("0x", "").rjust(64, "0")
        data = _BALANCE_OF_SELECTOR + padded_addr
        result = self._rpc_call(
            "eth_call", [{"to": contract, "data": data}, "latest"]
        )
        raw = result.get("result", "0x0")
        try:
            value = int(raw, 16)
        except (ValueError, TypeError):
            return 0.0
        return value / (10**decimals)

    def _get_beacon_validators(self, address: str) -> list[dict]:
        """Return raw validator dicts (pubkey, index) linked to this address
        as depositor or 0x01 withdrawal-credential recipient."""
        params = {"apikey": _beacon_key()} if _beacon_key() else {}
        resp = requests.get(
            f"{BEACONCHAIN_API}/validator/eth1/{address}", params=params, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("data", [])
        if isinstance(result, dict):  # API returns a dict for a single match
            result = [result]
        return result or []

    def _get_beacon_balances(self, indices: list[int]) -> list[dict]:
        """Return {publicvalidatorindex/pubkey, balance (Gwei), status} for
        each validator index, batched into one call."""
        params = {"apikey": _beacon_key()} if _beacon_key() else {}
        ids = ",".join(str(i) for i in indices)
        resp = requests.get(
            f"{BEACONCHAIN_API}/validator/{ids}", params=params, timeout=15
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if isinstance(data, dict):
            data = [data]
        return data or []

    def get_balance(self, address: str) -> WalletBalance:
        address = address.strip()
        wallet = WalletBalance(
            chain=self.chain_id, address=address, native_symbol=self.native_symbol
        )

        try:
            wallet.native_amount = self._get_native_balance(address)
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as exc:
            wallet.error = f"RPC error: {exc}"
            return wallet

        warnings: list[str] = []

        # ERC-20 tokens (needs Etherscan key to discover which contracts to check).
        if not _etherscan_key():
            warnings.append(
                "ETHERSCAN_API_KEY not set: ERC-20 token balances skipped"
            )
        else:
            try:
                contracts = self._discover_token_contracts(address)
            except requests.RequestException as exc:
                warnings.append(f"Etherscan error, tokens skipped: {exc}")
                contracts = {}

            for contract, meta in contracts.items():
                try:
                    amount = self._get_token_balance(
                        address, contract, meta["decimals"]
                    )
                except (requests.RequestException, RuntimeError):
                    continue
                if amount > 0:
                    wallet.tokens.append(
                        TokenBalance(
                            symbol=meta["symbol"],
                            name=meta["name"],
                            amount=amount,
                            contract=contract,
                        )
                    )

        # Native beacon-chain staking (32-ETH validators), invisible to
        # eth_getBalance / ERC-20 checks since it lives on the beacon chain.
        try:
            validators = self._get_beacon_validators(address)
            indices = [
                v["validatorindex"]
                for v in validators
                if v.get("validatorindex") is not None
            ]
            if indices:
                balances = self._get_beacon_balances(indices)
                for v in balances:
                    try:
                        gwei = int(v.get("balance", 0))
                    except (ValueError, TypeError):
                        continue
                    if gwei <= 0:
                        continue
                    idx = v.get("validatorindex", "?")
                    status = v.get("status", "unknown")
                    wallet.tokens.append(
                        TokenBalance(
                            symbol=self.native_symbol,
                            name=f"Staked {self.native_symbol} (validator #{idx}, {status})",
                            amount=gwei / 1e9,
                            contract=v.get("pubkey"),
                            asset_type="beacon-stake",
                        )
                    )
        except requests.RequestException as exc:
            warnings.append(f"could not fetch beacon-chain staking: {exc}")

        if warnings:
            wallet.warning = "; ".join(warnings)

        return wallet
