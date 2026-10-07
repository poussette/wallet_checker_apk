"""Valuation of MultiversX liquidity-pool (LP) tokens by reading the pool's
smart contract (`vm-values/query` on a MultiversX gateway).

Why contracts and not a DEX API: no public price exists for most LP tokens
(AshSwap, JEX, OneDex...), but the *pool* knows its reserves and the LP total
supply, so   LP price = value of the reserves / LP supply.

Finding the pool (real-world findings, see README): the account that *issued*
an LP token is often a router / factory, not the pool. The contract that really
mints and burns the LP token is, however, the pool, and the public endpoint
`/tokens/<LP>/roles` lists it (holder of ESDTRoleLocalMint / LocalBurn). So the
candidates for an LP token are: the holders of those roles + the issuer.

Reading the pool: a contract publishes no ABI, so each *adapter* below is data
(candidate view names for one family of contracts). A result is accepted only if
it is **consistent with independent on-chain facts**:

  * the pool itself names the LP token we hold (mandatory, no exception);
  * every reserve token is really held by the contract and the reserve does
    not exceed the contract's actual balance of that token;
  * the LP supply given by the pool agrees with minted - burnt from the API.

A wrong guess therefore yields "no value", never a wrong value.

Adding a DEX that is not recognised: run `python lp_probe.py <LP-identifier>`
(shows the candidate contracts and which views answer), then add a small dict to
ADAPTERS below. See README, section "LP tokens".
"""

from __future__ import annotations

import base64
import binascii
import math
import os
import re
import time
from decimal import Decimal
from urllib.parse import quote

import requests

from .net import HttpError, request_json
from .safe import MAX_POSITION_USD, safe_decimals, validate_rpc_url

DEFAULT_GATEWAY = "https://gateway.multiversx.com"
MVX_API = "https://api.multiversx.com"

#: hard budgets per run, so a hostile/unknown contract cannot cost minutes.
MAX_LP_TOKENS = 40
MAX_VM_CALLS = 300
MAX_OWNER_CALLS = 60          # per candidate contract (unknown contracts stay cheap)
MAX_CANDIDATES = 4            # candidate pool contracts per LP token
MAX_RETURN_ITEMS = 5000       # OneDex returns one item per pair (~1500 today)
MAX_RESPONSE_BYTES = 4_000_000
DEADLINE_SECONDS = 60         # wall clock for the whole LP step
MAX_SUPPLY_RAW = 10**60       # anything above is hostile metadata
BALANCE_TOLERANCE = 1.05      # indexed API balances can lag the live gateway
LP_MAX_POSITION_USD = 5e7     # a personal LP position above this is not credible

_TOKEN_ID_RE = re.compile(r"[A-Z0-9]{3,10}-[0-9a-f]{6}")  # use fullmatch
_SC_PREFIX = "erd1qqqqqqqq"  # smart-contract addresses start with 8 zero bytes
_LP_WORDS = ("lp", "liquidity", "pool")


def gateway_url() -> str:
    """Custom node (env MULTIVERSX_GATEWAY_URL, https only) or the public one."""
    url = validate_rpc_url(os.environ.get("MULTIVERSX_GATEWAY_URL")) or DEFAULT_GATEWAY
    return url.rstrip("/")


# --------------------------------------------------------------------- adapters
# Pure data: lists of candidate view names, tried in order. "curve" says how an
# unpriced side may be treated: constant_product (2 tokens, 50/50 value) or
# multi (stable / n-token pools: every token must be priced).

ADAPTERS: list[dict] = [
    {   # xExchange pair contract (open source: mx-exchange-sc) and its forks.
        "name": "xexchange-pair", "kind": "pair3", "curve": "constant_product",
        "code_hashes": ["PT7gHWG9n6lGmLjk2NpHNsaAX/n9P/cL+xPjljMCvXk="],
        "lp": ["getLpTokenIdentifier"],
        "first": ["getFirstTokenId"],
        "second": ["getSecondTokenId"],
        "reserves_and_supply": ["getReservesAndTotalSupply"],
    },
    {   # JEX: one dedicated contract per pair, separate views.
        "name": "jex-pair", "kind": "named", "curve": "constant_product",
        "code_hashes": ["kDh8hR9vyceELMUuy6JdAg0X90+ZaLeyVQS6tPbY82s="],
        "lp": ["getLpToken"],
        "first": ["getFirstToken"],
        "second": ["getSecondToken"],
        "first_reserve": ["getFirstTokenReserve"],
        "second_reserve": ["getSecondTokenReserve"],
        "supply": ["getLpTokenSupply"],
    },
    {   # OneDex: ONE contract for ~1000 pairs, views keyed by a numeric pair id,
        # the id being found in an LP-token -> id map.
        "name": "onedex", "kind": "keyed", "curve": "constant_product",
        "code_hashes": ["3ujBhmT9t7oZ4SbXL6agDT8fFMIzl5QEgr1arlVtvFE="],
        "id_map": ["getLpTokenPairIdMap"],
        "lp": ["getPairLpTokenId"],
        "first": ["getPairFirstTokenId"],
        "second": ["getPairSecondTokenId"],
        "first_reserve": ["getPairFirstTokenReserve"],
        "second_reserve": ["getPairSecondTokenReserve"],
        "supply": ["getPairLpTokenTotalSupply"],
    },
    {   # AshSwap and other pools exposing a token list (+ optional reserve list).
        # Stable pools: when no reserve view answers, the contract's own balances
        # of the listed tokens are the reserves (the pool still has to name the
        # LP token and give a supply that matches the chain).
        "name": "list-pool", "kind": "lists", "curve": "multi",
        "code_hashes": ["tc+lTL/W5PLTIgVVjK/qbL0UNQ9bdHFFdVAFHGME7xA="],
        "lp": ["getLpTokenIdentifier", "getLpTokenId", "getLpToken"],
        "tokens": ["getTokens", "getPoolTokens", "getTokenIds", "getUnderlyingTokens"],
        "reserves": ["getBalances", "getReserves", "getPoolReserves", "getTokenReserves"],
        "supply": ["getTotalSupply", "getLpTotalSupply", "getLpSupply", "getTotalLpSupply"],
    },
]

# "code_hashes": contract code hashes observed on the real pools of that DEX. A
# self-consistent pool with another code is still valued, but flagged "contrat
# non vérifié" and never with the 50/50 doubling: anyone can deploy a contract
# that answers these views with invented numbers (see README, "Limites").

#: view names tried by lp_probe.py to help writing a new adapter.
PROBE_NAMES = [
    "getReservesAndTotalSupply", "getReserves", "getReserve", "getFirstTokenId",
    "getSecondTokenId", "getLpTokenIdentifier", "getLpTokenId", "getLpToken",
    "getTotalSupply", "getLpTotalSupply", "getLpTokenSupply", "getTokens", "getTokenIds",
    "getPoolTokens", "getUnderlyingTokens", "getPoolReserves", "getTokenReserves",
    "getBalances", "getPairs", "viewPair", "viewPairs", "getState", "getPoolInfo",
    "getAmplificationFactor", "getAmpFactor", "getVirtualPrice", "getTotalFeePercent",
    "getSwapFeePercent", "getFirstToken", "getSecondToken", "getFirstTokenReserve",
    "getSecondTokenReserve", "getLpTokenPairIdMap", "getLastPairId",
]


# ------------------------------------------------------------------- low level

class Budget:
    """Call budget (optionally nested under a parent) plus a wall-clock deadline."""

    def __init__(self, calls: int = MAX_VM_CALLS, parent: "Budget | None" = None,
                 deadline: float | None = None):
        self.calls = calls
        self.parent = parent
        self.deadline = deadline if deadline is not None else time.monotonic() + DEADLINE_SECONDS

    def spend(self) -> bool:
        if self.calls <= 0 or time.monotonic() > self.deadline:
            return False
        if self.parent is not None and not self.parent.spend():
            return False
        self.calls -= 1
        return True


def _find_return(doc, depth: int = 0):
    if isinstance(doc, dict):
        if "returnData" in doc:
            return doc
        if depth < 3:
            for v in doc.values():
                found = _find_return(v, depth + 1)
                if found is not None:
                    return found
    return None


def vm_query(sc: str, func: str, args: list[str] | None = None, budget: Budget | None = None):
    """Call a view. Returns a list of raw byte strings, or None if the contract
    has no such view / it failed (so adapters can try the next name). Network
    errors propagate (requests.RequestException)."""
    if budget is not None and not budget.spend():
        raise requests.RequestException("LP query budget exhausted")
    try:
        doc = request_json(
            "POST", f"{gateway_url()}/vm-values/query",
            json_body={"scAddress": sc, "funcName": func, "args": list(args or [])},
            max_bytes=MAX_RESPONSE_BYTES, allow_loopback_http=True,
        )
    except HttpError as exc:
        # some gateways answer "unknown function" with a 4xx/500: that only
        # means "no such view"; rate limits / outages must still propagate.
        if re.match(r"HTTP (4\d\d|500) ", str(exc)) and not str(exc).startswith("HTTP 429"):
            return None
        raise
    ret = _find_return(doc)
    if ret is None or str(ret.get("returnCode", "")).lower() != "ok":
        return None
    data = ret.get("returnData") or []
    if not isinstance(data, list) or len(data) > MAX_RETURN_ITEMS:
        return None
    out: list[bytes] = []
    for item in data:
        try:
            out.append(base64.b64decode(item, validate=True) if isinstance(item, str) else b"")
        except (binascii.Error, ValueError):
            raise ValueError("malformed contract answer") from None
    return out


def as_uint(raw: bytes) -> int | None:
    return int.from_bytes(raw, "big") if len(raw) <= 32 else None


def as_token_id(raw: bytes) -> str | None:
    try:
        s = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    return s if _TOKEN_ID_RE.fullmatch(s) else None


def _blob_items(raw: bytes, limit: int = 8) -> list[bytes] | None:
    """A Vec encoded as one item: repeated (u32 length + bytes), nothing left over."""
    items: list[bytes] = []
    p = 0
    while p < len(raw):
        if len(items) >= limit or p + 4 > len(raw):
            return None
        n = int.from_bytes(raw[p:p + 4], "big")
        if n == 0 or n > 64 or p + 4 + n > len(raw):
            return None
        items.append(raw[p + 4:p + 4 + n])
        p += 4 + n
    return items or None


def _token_list(ret: list[bytes]) -> list[str] | None:
    """Token ids from either several items or one length-prefixed blob."""
    if not ret:
        return None
    if len(ret) == 1:
        parts = _blob_items(ret[0])
        if parts is None:
            return None
    else:
        parts = ret
    ids = [as_token_id(x) for x in parts]
    if len(ids) < 2 or None in ids or len(set(ids)) != len(ids):
        return None
    return ids  # type: ignore[return-value]


def _uint_list(ret: list[bytes], n: int) -> list[int] | None:
    """n integers from either n items or one length-prefixed blob."""
    if len(ret) == 1 and n > 1:
        parts = _blob_items(ret[0], limit=n)
        if parts is None:
            return None
    else:
        parts = ret
    if len(parts) != n:
        return None
    vals = [as_uint(x) for x in parts]
    return None if None in vals else vals  # type: ignore[return-value]


def _arg_uint(n: int) -> str:
    h = format(n, "x")
    return h if len(h) % 2 == 0 else "0" + h


def _first_answer(sc, names, args, budget):
    """(name, returnData) of the first view in `names` that answers."""
    for name in names:
        res = vm_query(sc, name, args, budget)
        if res:
            return name, res
    return None, None


# ------------------------------------------------------------------ chain facts

def _token_facts(lp: str):
    """{owner, supply_raw, decimals} for the LP token, or None."""
    d = request_json("GET", f"{MVX_API}/tokens/{quote(lp, safe='')}")
    if not isinstance(d, dict):
        return None
    decimals = safe_decimals(d.get("decimals"))
    try:
        supply = int(d.get("initialMinted") or 0) + int(d["minted"]) - int(d["burnt"])
        if decimals is not None and d.get("supply") is not None:
            # second source: the API's own (display-unit) supply must agree
            shown = Decimal(str(d["supply"])) * (10 ** decimals)
            if abs(shown - supply) > max(supply // 50, 10 ** decimals):
                return None
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None
    owner = d.get("owner")
    if not 0 < supply <= MAX_SUPPLY_RAW or decimals is None:
        return None
    return {
        "owner": owner if isinstance(owner, str) and owner.startswith(_SC_PREFIX) else None,
        "supply_raw": supply, "decimals": decimals,
    }


def candidate_contracts(lp: str, facts: dict) -> list[str]:
    """Smart contracts that may be the pool: holders of the LP mint/burn roles,
    then the issuer. (The issuer is often a router/factory, the role holder the pool.)"""
    out: list[str] = []
    try:
        roles = request_json("GET", f"{MVX_API}/tokens/{quote(lp, safe='')}/roles")
    except HttpError:
        roles = None  # endpoint absent for this token: the issuer is still tried
    for r in roles if isinstance(roles, list) else []:
        if not isinstance(r, dict):
            continue
        addr, rl = r.get("address"), r.get("roles")
        if (
            isinstance(addr, str) and addr.startswith(_SC_PREFIX) and isinstance(rl, list)
            and any(x in ("ESDTRoleLocalMint", "ESDTRoleLocalBurn") for x in rl)
            and addr not in out
        ):
            out.append(addr)
    if facts["owner"] and facts["owner"] not in out:
        out.append(facts["owner"])
    return out[:MAX_CANDIDATES]


def _balance(sc: str, token: str) -> int:
    """The contract's real balance of one fungible token (0 if it holds none)."""
    try:
        d = request_json("GET", f"{MVX_API}/accounts/{quote(sc, safe='')}/tokens/{quote(token, safe='')}")
    except HttpError as exc:
        if str(exc).startswith("HTTP 404"):
            return 0
        raise
    try:
        return max(0, int(d.get("balance", 0))) if isinstance(d, dict) else 0
    except (TypeError, ValueError):
        return 0


class PoolState:
    def __init__(self, adapter: str, reserves: dict[str, int], curve: str,
                 verified: bool = False, from_balances: bool = False):
        self.adapter = adapter
        self.reserves = reserves
        self.curve = curve  # "constant_product" (2 tokens) or "multi"
        self.verified = verified          # contract code hash is a known one
        self.from_balances = from_balances  # reserves = contract balances, not a view


class Inconsistent(Exception):
    """The pool answered, but its answer contradicts on-chain facts."""


def _supply_ok(a: int, b: int) -> bool:
    return abs(a - b) <= max(b // 50, 1000)


def _within(reserve: int, balance: int) -> bool:
    """reserve <= balance, with slack: balances come from the indexed API,
    reserves live from the gateway."""
    return reserve <= int(balance * BALANCE_TOLERANCE) + 1


def _code_hash(sc: str, budget: Budget) -> str | None:
    if not budget.spend():
        raise requests.RequestException("LP query budget exhausted")
    d = request_json("GET", f"{MVX_API}/accounts/{quote(sc, safe='')}", params={"fields": "codeHash"})
    h = d.get("codeHash") if isinstance(d, dict) else None
    return h if isinstance(h, str) and len(h) <= 64 else None


def _verify(spec, sc, facts, reserves: dict[str, int], supply: int, budget: Budget,
            from_balances: bool = False) -> PoolState:
    if len(reserves) < 2 or any(v <= 0 for v in reserves.values()):
        raise Inconsistent("unreadable reserves")
    if not _supply_ok(supply, facts["supply_raw"]):
        raise Inconsistent("pool supply contradicts the token supply")
    for tok, res in reserves.items():
        if not budget.spend():
            raise requests.RequestException("LP query budget exhausted")
        if not _within(res, _balance(sc, tok)):
            raise Inconsistent("reserve exceeds the contract's balance")
    verified = _code_hash(sc, budget) in spec.get("code_hashes", ())
    return PoolState(spec["name"], dict(reserves), spec["curve"], verified, from_balances)


def _names_lp(spec, sc, lp, budget, args) -> bool | None:
    """True: the pool names this LP. None: no answer. Inconsistent: names another."""
    _, ans = _first_answer(sc, spec["lp"], args, budget)
    if not ans:
        return None
    if as_token_id(ans[0]) != lp:
        raise Inconsistent("pool names another LP token")
    return True


# --------------------------------------------------------------------- strategies

def _try_pair3(spec, lp, facts, sc, ctx, budget):
    if not _names_lp(spec, sc, lp, budget, []):
        return None
    _, a = _first_answer(sc, spec["first"], [], budget)
    _, b = _first_answer(sc, spec["second"], [], budget)
    if not a or not b:
        return None
    t1, t2 = as_token_id(a[0]), as_token_id(b[0])
    if not t1 or not t2 or t1 == t2:
        raise Inconsistent("bad pool tokens")
    _, rs = _first_answer(sc, spec["reserves_and_supply"], [], budget)
    if not rs or len(rs) < 3:
        return None
    r1, r2, sup = as_uint(rs[0]), as_uint(rs[1]), as_uint(rs[2])
    if None in (r1, r2, sup):
        raise Inconsistent("unreadable reserves")
    return _verify(spec, sc, facts, {t1: r1, t2: r2}, sup, budget)


def _try_named(spec, lp, facts, sc, ctx, budget):
    if not _names_lp(spec, sc, lp, budget, []):
        return None
    got = {}
    for key in ("first", "second", "first_reserve", "second_reserve", "supply"):
        _, ans = _first_answer(sc, spec[key], [], budget)
        if not ans:
            return None
        got[key] = ans[0]
    t1, t2 = as_token_id(got["first"]), as_token_id(got["second"])
    r1, r2, sup = as_uint(got["first_reserve"]), as_uint(got["second_reserve"]), as_uint(got["supply"])
    if not t1 or not t2 or t1 == t2 or None in (r1, r2, sup):
        raise Inconsistent("unreadable pool answers")
    return _verify(spec, sc, facts, {t1: r1, t2: r2}, sup, budget)


def _try_keyed(spec, lp, facts, sc, ctx, budget):
    if "id_map" not in ctx:
        _, m = _first_answer(sc, spec["id_map"], [], budget)
        mapping: dict[str, int] = {}
        for i in range(0, len(m or []) - 1, 2):
            tid, num = as_token_id(m[i]), as_uint(m[i + 1])
            if tid and num is not None:
                mapping[tid] = num
        ctx["id_map"] = mapping
    pid = ctx["id_map"].get(lp)
    if pid is None:
        return None
    args = [_arg_uint(pid)]
    if not _names_lp(spec, sc, lp, budget, args):
        raise Inconsistent("pair id does not lead to this LP")
    got = {}
    for key in ("first", "second", "first_reserve", "second_reserve", "supply"):
        _, ans = _first_answer(sc, spec[key], args, budget)
        if not ans:
            return None
        got[key] = ans[0]
    t1, t2 = as_token_id(got["first"]), as_token_id(got["second"])
    r1, r2, sup = as_uint(got["first_reserve"]), as_uint(got["second_reserve"]), as_uint(got["supply"])
    if not t1 or not t2 or t1 == t2 or None in (r1, r2, sup):
        raise Inconsistent("unreadable pool answers")
    return _verify(spec, sc, facts, {t1: r1, t2: r2}, sup, budget)


def _try_lists(spec, lp, facts, sc, ctx, budget):
    if not _names_lp(spec, sc, lp, budget, []):
        return None
    _, tk = _first_answer(sc, spec["tokens"], [], budget)
    ids = _token_list(tk or [])
    if not ids:
        return None
    _, sp = _first_answer(sc, spec["supply"], [], budget)
    sup = as_uint(sp[0]) if sp else None
    if sup is None:
        return None  # without a supply the pool says nothing we can cross-check
    _, rs = _first_answer(sc, spec["reserves"], [], budget)
    vals = _uint_list(rs, len(ids)) if rs else None
    from_balances = vals is None
    if vals is None:
        # no usable reserve view: the contract's own balances are the reserves
        vals = []
        for t in ids:
            if not budget.spend():
                raise requests.RequestException("LP query budget exhausted")
            vals.append(_balance(sc, t))
    return _verify(spec, sc, facts, dict(zip(ids, vals)), sup, budget, from_balances)


_STRATEGY = {"pair3": _try_pair3, "named": _try_named, "keyed": _try_keyed, "lists": _try_lists}


def discover_pool(lp: str, budget: Budget, cache: dict) -> tuple[dict, PoolState] | None:
    facts = _token_facts(lp)
    if not facts:
        return None
    for sc in candidate_contracts(lp, facts):
        ctx = cache.setdefault(sc, {
            "dead": False, "spec": None,
            "budget": Budget(MAX_OWNER_CALLS, parent=budget, deadline=budget.deadline),
        })
        if ctx["dead"]:
            continue
        specs = ([ctx["spec"]] if ctx["spec"] else []) + [s for s in ADAPTERS if s is not ctx["spec"]]
        answered = False
        for spec in specs:
            try:
                state = _STRATEGY[spec["kind"]](spec, lp, facts, sc, ctx, ctx["budget"])
            except Inconsistent:
                answered = True
                break  # answered but contradicted by the chain: distrust this contract
            except requests.RequestException:
                raise  # budget / deadline / outage: the caller leaves this LP unvalued
            if state:
                ctx["spec"] = spec
                return facts, state
        if not answered and ctx["spec"] is None and not ctx.get("id_map"):
            ctx["dead"] = True  # speaks none of the known dialects: skip for other LPs
    return None


# ---------------------------------------------------------------------- pricing

def pool_value_detail(state: PoolState, infos: dict) -> tuple[float, bool] | None:
    """(USD value of all reserves, estimated?) from `infos` =
    {token_id: {"price", "decimals"}}. `estimated` = one side unpriced, doubled."""
    parts: list[float] = []
    unpriced = 0
    for tid, raw in state.reserves.items():
        info = infos.get(tid) or {}
        dec = safe_decimals(info.get("decimals"))
        if dec is None:
            return None
        try:
            amount = raw / (10 ** dec)
        except OverflowError:
            return None
        price = info.get("price")
        if price:
            parts.append(amount * price)
        else:
            unpriced += 1
    if not parts:
        return None
    estimated = False
    if unpriced == 0:
        value = sum(parts)
    elif (state.verified and state.curve == "constant_product"
          and len(state.reserves) == 2 and unpriced == 1):
        value = 2 * parts[0]  # a 50/50 constant-product pool: both sides worth the same
        estimated = True
    else:
        return None
    if math.isfinite(value) and 0 < value <= MAX_POSITION_USD:
        return value, estimated
    return None


def pool_value_usd(state: PoolState, infos: dict) -> float | None:
    res = pool_value_detail(state, infos)
    return res[0] if res else None


def looks_like_lp(symbol: str, name: str) -> bool:
    text = f"{symbol} {name}".lower()
    return any(w in text for w in _LP_WORDS)


def price_lp_tokens(candidates: list[str], fetch_infos) -> dict[str, dict]:
    # `candidates` should be ordered most-valuable-first (the per-run limits
    # then spare the real positions, not the alphabetically early spam).
    """{lp_identifier: {"usd": unit price, "adapter": label}} for the LP tokens
    among `candidates` that could be valued. `fetch_infos(ids)` must return
    {token_id: {"price": usd|None, "decimals": int}} (pricing.py provides it).
    Never raises: any failure just leaves that LP unvalued."""
    budget = Budget()
    cache: dict = {}
    found: list[tuple[str, dict, PoolState]] = []
    ordered = list(dict.fromkeys(candidates))[:MAX_LP_TOKENS]
    for lp in ordered:
        if not _TOKEN_ID_RE.fullmatch(lp):
            continue
        try:
            res = discover_pool(lp, budget, cache)
        except Exception:  # noqa: BLE001 - hostile data must never abort the pricing run
            res = None
        if res:
            found.append((lp, res[0], res[1]))
    if not found:
        return {}
    ids = {t for _, _, st in found for t in st.reserves}
    try:
        infos = fetch_infos(ids)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict] = {}
    for lp, facts, st in found:
        detail = pool_value_detail(st, infos)
        if detail is None:
            continue
        value, estimated = detail
        try:
            supply = facts["supply_raw"] / (10 ** facts["decimals"])
            price = value / supply if supply > 0 else None
        except (OverflowError, ZeroDivisionError):
            continue
        if price and math.isfinite(price) and price > 0:
            label = st.adapter
            if estimated:
                label += ", estimation 50/50"
            if st.from_balances:
                label += ", soldes du contrat"
            if not st.verified:
                label += ", contrat non vérifié"
            out[lp] = {"usd": price, "adapter": label, "supply": supply}
    return out
