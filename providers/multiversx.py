"""MultiversX (formerly Elrond) provider.

Backed by the free public MultiversX REST API (no key required).
Docs: https://api.multiversx.com/#/

MultiversX has several distinct asset kinds on top of the native EGLD coin:
  - FungibleESDT : a regular fungible token (e.g. USDC, WTAO, MEX, ...)
  - MetaESDT     : a fungible-like token that also carries extra on-chain
                    metadata (typically LP / liquid-staking tokens)
  - NonFungibleESDT (NFT) : one-of-a-kind, amount is always 1, decimals 0
  - SemiFungibleESDT (SFT): fungible within its own nonce, decimals usually 0

Each TokenBalance.asset_type below is set to "esdt", "meta-esdt", "nft" or
"sft" accordingly, so the caller can tell them apart.

EGLD staked through delegation or direct validator staking is held by
dedicated smart contracts, not the wallet itself, so it never shows up in
the plain account balance. This provider also queries:
  - /accounts/{address}/delegation        (stake delegated to staking pools,
    one entry per pool contract, "esdt" asset_type "delegation")
  - /accounts/{address}/delegation-legacy  (old, pre-staking-pools delegation)
  - /accounts/{address}/stake              (direct/validator staking, for
    addresses that run their own validator node)
and reports each as an EGLD-denominated TokenBalance with asset_type
"delegation", "delegation-legacy" or "validator-stake".

Undelegating EGLD (unstaked, but still working through the ~10-day unbonding
period before it can be withdrawn back to the wallet) also lives on the
delegation contract, not the wallet, so it's covered too:
  - "delegation-unbonding"   : still cooling down (userUndelegatedList), name
                                includes the estimated days left
  - "delegation-unbondable"  : cooldown finished, ready for an explicit
                                withdraw transaction (userUnBondable)
"""

from __future__ import annotations

import re

import requests

from .base import BaseProvider, TokenBalance, WalletBalance

MVX_API = "https://api.multiversx.com"
PAGE_SIZE = 100  # max allowed by the public API

_MVX_RE = re.compile(r"^erd1[a-z0-9]{58}$")

# Maps the API's "type" field to our normalized asset_type.
_TYPE_MAP = {
    "FungibleESDT": "esdt",
    "MetaESDT": "meta-esdt",
    "NonFungibleESDT": "nft",
    "SemiFungibleESDT": "sft",
}


class MultiversXProvider(BaseProvider):
    chain_id = "multiversx"
    display_name = "MultiversX"
    native_symbol = "EGLD"

    @classmethod
    def matches(cls, address: str) -> bool:
        return bool(_MVX_RE.match(address.strip()))

    def _paginate(self, path: str) -> list[dict]:
        """GET all pages of an MultiversX list endpoint (size/from pagination)."""
        items: list[dict] = []
        offset = 0
        while True:
            resp = requests.get(
                f"{MVX_API}{path}",
                params={"from": offset, "size": PAGE_SIZE},
                timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        return items

    def _to_token_balance(self, tok: dict) -> TokenBalance | None:
        try:
            decimals = int(tok.get("decimals", 0))
            raw_balance = int(tok.get("balance", 0))
            amount = raw_balance / (10**decimals) if decimals else float(raw_balance)
        except (ValueError, TypeError):
            return None
        if amount <= 0:
            return None
        api_type = tok.get("type", "FungibleESDT")
        return TokenBalance(
            symbol=tok.get("ticker", tok.get("identifier", "?")),
            name=tok.get("name", "Unknown token"),
            amount=amount,
            contract=tok.get("identifier"),
            asset_type=_TYPE_MAP.get(api_type, "esdt"),
        )

    def get_balance(self, address: str) -> WalletBalance:
        address = address.strip()
        wallet = WalletBalance(
            chain=self.chain_id, address=address, native_symbol=self.native_symbol
        )

        try:
            resp = requests.get(f"{MVX_API}/accounts/{address}", timeout=15)
            resp.raise_for_status()
            data = resp.json()
            wallet.native_amount = int(data.get("balance", 0)) / 1e18
        except requests.RequestException as exc:
            wallet.error = f"API error: {exc}"
            return wallet
        except (ValueError, TypeError) as exc:
            wallet.error = f"Unexpected response: {exc}"
            return wallet

        warnings: list[str] = []

        # Fungible + MetaESDT tokens (paginated: an account can hold more
        # than the API's default page size, which is how tokens go missing).
        try:
            fungible = self._paginate(f"/accounts/{address}/tokens")
            for tok in fungible:
                tb = self._to_token_balance(tok)
                if tb:
                    wallet.tokens.append(tb)
        except requests.RequestException as exc:
            warnings.append(f"could not fetch ESDT/MetaESDT tokens: {exc}")

        # NFTs and SFTs live on a separate endpoint.
        try:
            nfts_sfts = self._paginate(f"/accounts/{address}/nfts")
            for tok in nfts_sfts:
                tb = self._to_token_balance(tok)
                if tb:
                    wallet.tokens.append(tb)
        except requests.RequestException as exc:
            warnings.append(f"could not fetch NFTs/SFTs: {exc}")

        # --- Staked EGLD held by staking smart contracts, not the wallet ---

        # Delegation to staking pools (the common way to stake EGLD).
        try:
            resp = requests.get(f"{MVX_API}/accounts/{address}/delegation", timeout=15)
            resp.raise_for_status()
            for pos in resp.json():
                try:
                    staked = int(pos.get("userActiveStake", 0)) / 1e18
                except (ValueError, TypeError):
                    staked = 0
                if staked > 0:
                    wallet.tokens.append(
                        TokenBalance(
                            symbol=self.native_symbol,
                            name="Delegated EGLD (staking pool)",
                            amount=staked,
                            contract=pos.get("contract"),
                            asset_type="delegation",
                        )
                    )
                try:
                    rewards = float(pos.get("claimableRewards", 0)) / 1e18
                except (ValueError, TypeError):
                    rewards = 0
                if rewards > 0:
                    wallet.tokens.append(
                        TokenBalance(
                            symbol=self.native_symbol,
                            name="Claimable delegation rewards",
                            amount=rewards,
                            contract=pos.get("contract"),
                            asset_type="delegation-rewards",
                        )
                    )

                # Already unbonded, ready for an explicit withdraw tx.
                try:
                    unbondable = int(pos.get("userUnBondable", 0)) / 1e18
                except (ValueError, TypeError):
                    unbondable = 0
                if unbondable > 0:
                    wallet.tokens.append(
                        TokenBalance(
                            symbol=self.native_symbol,
                            name="Undelegated EGLD, ready to withdraw",
                            amount=unbondable,
                            contract=pos.get("contract"),
                            asset_type="delegation-unbondable",
                        )
                    )

                # Undelegated but still cooling down (unbonding period).
                # Once the cooldown reaches 0, the API keeps the entry in
                # userUndelegatedList (with seconds == 0) *in addition to*
                # moving the same amount into userUnBondable above -- so a
                # matured entry here would double-count money already
                # reported as "delegation-unbondable". Skip anything at or
                # past maturity; only genuinely still-cooling-down amounts
                # are reported here.
                for entry in pos.get("userUndelegatedList", []) or []:
                    try:
                        amount = int(entry.get("amount", 0)) / 1e18
                    except (ValueError, TypeError):
                        amount = 0
                    if amount <= 0:
                        continue
                    seconds_left = entry.get("seconds")
                    try:
                        seconds_left = int(seconds_left) if seconds_left is not None else None
                    except (ValueError, TypeError):
                        seconds_left = None
                    if seconds_left is not None and seconds_left <= 0:
                        # Already matured -> already counted via
                        # userUnBondable, don't double-count it here.
                        continue
                    days_left = (
                        round(seconds_left / 86400, 1) if seconds_left is not None else None
                    )
                    name = "Undelegated EGLD, unbonding" + (
                        f" (~{days_left}j restants)" if days_left is not None else ""
                    )
                    wallet.tokens.append(
                        TokenBalance(
                            symbol=self.native_symbol,
                            name=name,
                            amount=amount,
                            contract=pos.get("contract"),
                            asset_type="delegation-unbonding",
                        )
                    )
        except requests.RequestException as exc:
            warnings.append(f"could not fetch delegation: {exc}")

        # Legacy (pre-staking-pools) delegation, still used by some addresses.
        try:
            resp = requests.get(
                f"{MVX_API}/accounts/{address}/delegation-legacy", timeout=15
            )
            resp.raise_for_status()
            legacy = resp.json()
            try:
                legacy_staked = int(legacy.get("userStake", 0)) / 1e18
            except (ValueError, TypeError):
                legacy_staked = 0
            if legacy_staked > 0:
                wallet.tokens.append(
                    TokenBalance(
                        symbol=self.native_symbol,
                        name="Delegated EGLD (legacy delegation)",
                        amount=legacy_staked,
                        contract="legacy-delegation",
                        asset_type="delegation-legacy",
                    )
                )
            # Best-effort: the legacy delegation endpoint's exact field names
            # for in-progress unbonding are less consistently documented
            # than the staking-pool endpoint above, so this is a defensive
            # attempt rather than a guaranteed match -- it silently yields
            # nothing if the field isn't present, rather than erroring.
            try:
                legacy_undelegated = int(legacy.get("userUnDelegatedValue", 0)) / 1e18
            except (ValueError, TypeError):
                legacy_undelegated = 0
            if legacy_undelegated > 0:
                wallet.tokens.append(
                    TokenBalance(
                        symbol=self.native_symbol,
                        name="Undelegated EGLD (legacy delegation, unbonding or ready to withdraw)",
                        amount=legacy_undelegated,
                        contract="legacy-delegation",
                        asset_type="delegation-legacy-unbonding",
                    )
                )
        except requests.RequestException as exc:
            warnings.append(f"could not fetch legacy delegation: {exc}")

        # Direct validator staking (running your own node).
        try:
            resp = requests.get(f"{MVX_API}/accounts/{address}/stake", timeout=15)
            resp.raise_for_status()
            stake_data = resp.json()
            try:
                validator_staked = int(stake_data.get("totalStaked", 0)) / 1e18
            except (ValueError, TypeError):
                validator_staked = 0
            if validator_staked > 0:
                wallet.tokens.append(
                    TokenBalance(
                        symbol=self.native_symbol,
                        name="Staked EGLD (own validator node)",
                        amount=validator_staked,
                        contract="validator-staking",
                        asset_type="validator-stake",
                    )
                )
        except requests.RequestException as exc:
            warnings.append(f"could not fetch validator stake: {exc}")

        if warnings:
            wallet.warning = "; ".join(warnings)

        return wallet
