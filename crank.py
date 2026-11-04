#!/usr/bin/env python3
"""Update the stake pool for the current epoch, packing many validators into each transaction.

Does what `spl-stake-pool update --stale-only` does, built to put up with a flaky RPC:

- Each transaction updates as many validators as fit (11; the CLI uses 5), so a full
  crank is ~185 transactions instead of ~405.
- It never calls getFeeForMessage, the call behind the CLI's "Custom: Invalid blockhash".
- It signs with a *finalized* blockhash, which every node behind a load-balanced RPC already
  knows, so a node that lags a little still accepts it.
- A transaction that fails to send or land is simply rebuilt next round; the chain is the
  only state. Each round re-reads the validator list and sends only what is still stale.

Env: RPC_URL, FEE_PAYER_KEYPAIR (the [..64 numbers..] keypair JSON), STAKE_POOL,
COMPUTE_UNIT_PRICE (microlamports, default 50000), CRANK_BUDGET_SECONDS (default 1320).
DRY_RUN=1 simulates every transaction instead of sending it, checks that each one really
updates its validators, and needs only FEE_PAYER_PUBKEY (no keypair).
Exits 0 once the pool is current, 1 if it is still stale when the time budget runs out.
"""
import base64
import json
import os
import sys
import time
import urllib.request

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.sysvar import CLOCK, STAKE_HISTORY
from solders.transaction import Transaction

RPC = os.environ["RPC_URL"]
POOL = Pubkey.from_string(os.environ.get("STAKE_POOL", "DqhH94PjkZsjAqEze2BEkWhFQJ6EyU6MdtMphMgnXqeK"))
CU_PRICE = int(os.environ.get("COMPUTE_UNIT_PRICE") or "50000")
BUDGET = int(os.environ.get("CRANK_BUDGET_SECONDS") or "1320")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

STAKE_POOL_PROGRAM = Pubkey.from_string("SPoo1Ku8WFXoNDMHPsrGSTSG1Y47rzgn41SLUNakuHy")
STAKE_PROGRAM = Pubkey.from_string("Stake11111111111111111111111111111111111111")
PACKET_DATA_SIZE = 1232  # max serialized legacy transaction
MAX_VALIDATORS_PER_TX = 27  # (64 account locks - 10 fixed accounts) / 2
# Compute limit per transaction. Simulated on mainnet (epoch 1077): 11 validators took
# 72k-115k CU, depending on transient stake merges. The priority fee is charged on the
# limit, so keep ~1.7x headroom rather than asking for the max. The final pool balance
# update walks the whole validator list (~50k CU just to reach the staleness check).
CU_BASE = 30_000
CU_PER_VALIDATOR = 15_000
CU_FINAL = 400_000
CONFIRMED = {"commitment": "confirmed"}
ENTRY = 73  # ValidatorStakeInfo size
LIST_HEADER = 9  # account_type u8, max_validators u32, vec len u32


def log(msg):
    print(msg, flush=True)


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    if "error" in out:
        raise RpcError(f"{method}: {out['error'].get('message', out['error'])}")
    return out["result"]


class RpcError(Exception):
    pass


def account_data(address):
    value = rpc("getAccountInfo", [str(address), {"encoding": "base64", **CONFIRMED}])["value"]
    return base64.b64decode(value["data"][0])


def pk(data, off):
    return Pubkey.from_bytes(data[off : off + 32])


def u64(data, off):
    return int.from_bytes(data[off : off + 8], "little")


class Pool:
    """The StakePool account fields the update needs (spl-stake-pool 2.0 layout)."""

    def __init__(self, data):
        self.validator_list = pk(data, 98)
        self.reserve = pk(data, 130)
        self.mint = pk(data, 162)
        self.manager_fee_account = pk(data, 194)
        self.token_program = pk(data, 226)
        self.last_update_epoch = u64(data, 274)


def validator_entries(data):
    n = int.from_bytes(data[5:9], "little")
    out = []
    for i in range(n):
        e = data[LIST_HEADER + ENTRY * i : LIST_HEADER + ENTRY * (i + 1)]
        out.append(
            {
                "last_update_epoch": u64(e, 16),
                "transient_seed": u64(e, 24),
                "validator_seed": int.from_bytes(e[36:40], "little"),
                "vote": pk(e, 41),
            }
        )
    return out


WITHDRAW_AUTHORITY, _ = Pubkey.find_program_address([bytes(POOL), b"withdraw"], STAKE_POOL_PROGRAM)


def stake_accounts(entry):
    seeds = [bytes(entry["vote"]), bytes(POOL)]
    if entry["validator_seed"]:
        seeds.append(entry["validator_seed"].to_bytes(4, "little"))
    validator_stake, _ = Pubkey.find_program_address(seeds, STAKE_POOL_PROGRAM)
    transient_stake, _ = Pubkey.find_program_address(
        [b"transient", bytes(entry["vote"]), bytes(POOL), entry["transient_seed"].to_bytes(8, "little")],
        STAKE_POOL_PROGRAM,
    )
    return validator_stake, transient_stake


def update_list_ix(pool, entries, start):
    """UpdateValidatorListBalance for entries[start:start+len(entries)] (no_merge = false)."""
    accounts = [
        AccountMeta(POOL, False, False),
        AccountMeta(WITHDRAW_AUTHORITY, False, False),
        AccountMeta(pool.validator_list, False, True),
        AccountMeta(pool.reserve, False, True),
        AccountMeta(CLOCK, False, False),
        AccountMeta(STAKE_HISTORY, False, False),
        AccountMeta(STAKE_PROGRAM, False, False),
    ]
    for e in entries:
        v, t = stake_accounts(e)
        accounts += [AccountMeta(v, False, True), AccountMeta(t, False, True)]
    data = bytes([6]) + start.to_bytes(4, "little") + bytes([0])
    return Instruction(STAKE_POOL_PROGRAM, data, accounts)


def final_ixs(pool):
    """UpdateStakePoolBalance, then CleanupRemovedValidatorEntries, like the CLI."""
    balance = Instruction(
        STAKE_POOL_PROGRAM,
        bytes([7]),
        [
            AccountMeta(POOL, False, True),
            AccountMeta(WITHDRAW_AUTHORITY, False, False),
            AccountMeta(pool.validator_list, False, True),
            AccountMeta(pool.reserve, False, False),
            AccountMeta(pool.manager_fee_account, False, True),
            AccountMeta(pool.mint, False, True),
            AccountMeta(pool.token_program, False, False),
        ],
    )
    cleanup = Instruction(
        STAKE_POOL_PROGRAM,
        bytes([8]),
        [AccountMeta(POOL, False, False), AccountMeta(pool.validator_list, False, True)],
    )
    return [balance, cleanup]


class Signer:
    def __init__(self):
        if os.environ.get("FEE_PAYER_KEYPAIR"):
            self.keypair = Keypair.from_bytes(bytes(json.loads(os.environ["FEE_PAYER_KEYPAIR"])))
            self.pubkey = self.keypair.pubkey()
        elif DRY_RUN:
            self.keypair = None
            self.pubkey = Pubkey.from_string(os.environ["FEE_PAYER_PUBKEY"])
        else:
            raise SystemExit("FEE_PAYER_KEYPAIR is not set")

    def tx(self, ixs, cu_limit, blockhash):
        ixs = [set_compute_unit_limit(cu_limit), set_compute_unit_price(CU_PRICE), *ixs]
        msg = Message.new_with_blockhash(ixs, self.pubkey, blockhash)
        if self.keypair is None:
            return Transaction.new_unsigned(msg)
        return Transaction([self.keypair], msg, blockhash)


def fits(signer, ixs, cu_limit):
    return len(bytes(signer.tx(ixs, cu_limit, Hash.default()))) <= PACKET_DATA_SIZE


def plan_ranges(signer, pool, entries, epoch):
    """Cover every stale entry with contiguous ranges, each as long as fits in one tx."""
    stale = [i for i, e in enumerate(entries) if e["last_update_epoch"] < epoch]
    # Every validator adds the same two new accounts, so the size only depends on the count.
    per_tx = min(MAX_VALIDATORS_PER_TX, len(entries))
    while per_tx > 1 and not fits(signer, [update_list_ix(pool, entries[:per_tx], 0)], 1):
        per_tx -= 1
    ranges = []
    j = 0
    while j < len(stale):
        start = stale[j]
        n = min(per_tx, len(entries) - start)
        ranges.append((start, n))
        while j < len(stale) and stale[j] < start + n:
            j += 1
    return stale, ranges


def finalized_blockhash():
    # Finalized lags ~32 slots behind the tip, so every node behind the RPC has it, and it
    # still has ~45s of validity left: refresh it often (see BLOCKHASH_MAX_AGE).
    value = rpc("getLatestBlockhash", [{"commitment": "finalized"}])["value"]
    return Hash.from_string(value["blockhash"])


BLOCKHASH_MAX_AGE = 15  # seconds


def send(tx):
    encoded = base64.b64encode(bytes(tx)).decode()
    return rpc(
        "sendTransaction",
        [encoded, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 5}],
    )


def wait_for(signatures, timeout=60):
    """Poll until every signature is confirmed or failed, or the timeout. Returns (ok, failed)."""
    pending = set(signatures)
    ok = failed = 0
    end = time.monotonic() + timeout
    while pending and time.monotonic() < end:
        time.sleep(2)
        batch = list(pending)
        for k in range(0, len(batch), 256):
            chunk = batch[k : k + 256]
            try:
                statuses = rpc("getSignatureStatuses", [chunk])["value"]
            except (RpcError, OSError) as e:
                log(f"  getSignatureStatuses failed ({e}), will retry")
                continue
            for sig, st in zip(chunk, statuses):
                if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                    pending.discard(sig)
                    if st.get("err"):
                        failed += 1
                        log(f"  tx {sig} failed on chain: {st['err']}")
                    else:
                        ok += 1
    return ok, failed


def simulate(tx, validator_list):
    encoded = base64.b64encode(bytes(tx)).decode()
    return rpc(
        "simulateTransaction",
        [
            encoded,
            {
                "encoding": "base64",
                "sigVerify": False,
                "replaceRecentBlockhash": True,
                "commitment": "confirmed",
                "accounts": {"encoding": "base64", "addresses": [str(validator_list)]},
            },
        ],
    )["value"]


def dry_run(signer, pool, entries, epoch):
    stale, ranges = plan_ranges(signer, pool, entries, epoch)
    log(f"{len(entries)} validators, {len(stale)} stale, {len(ranges)} transactions")
    bad = 0
    for start, n in ranges:
        cu = CU_BASE + CU_PER_VALIDATOR * n
        tx = signer.tx([update_list_ix(pool, entries[start : start + n], start)], cu, Hash.default())
        res = simulate(tx, pool.validator_list)
        after = validator_entries(base64.b64decode(res["accounts"][0]["data"][0]))
        updated = sum(after[i]["last_update_epoch"] >= epoch for i in range(start, start + n))
        status = "ok" if not res["err"] and updated == n else "BAD"
        bad += status == "BAD"
        log(
            f"  [{start}..{start + n}) {len(bytes(tx))} bytes, {res['unitsConsumed']} of {cu} CU, "
            f"{updated}/{n} updated, err={res['err']} {status}"
        )
    # While validators are stale this fails with StakeListOutOfDate, which the program only
    # reaches after checking every account it was given.
    res = simulate(signer.tx(final_ixs(pool), CU_FINAL, Hash.default()), pool.validator_list)
    errors = [line for line in res["logs"] or [] if "Error" in line or "failed" in line]
    log(f"  pool balance update: {res['unitsConsumed']} of {CU_FINAL} CU, err={res['err']} {errors}")
    return 1 if bad else 0


def main():
    signer = Signer()
    deadline = time.monotonic() + BUDGET
    round_no = 0
    while True:
        round_no += 1
        try:
            epoch = rpc("getEpochInfo", [CONFIRMED])["epoch"]
            pool = Pool(account_data(POOL))
            if pool.last_update_epoch >= epoch and not DRY_RUN:
                log(f"pool is current (epoch {epoch})")
                return 0
            entries = validator_entries(account_data(pool.validator_list))
            if DRY_RUN:
                return dry_run(signer, pool, entries, epoch)

            stale, ranges = plan_ranges(signer, pool, entries, epoch)
            log(f"round {round_no}: epoch {epoch}, {len(stale)}/{len(entries)} validators stale, "
                f"{len(ranges)} transactions to send")
            sigs = []
            if ranges:
                blockhash, fetched = finalized_blockhash(), time.monotonic()
                for start, n in ranges:
                    if time.monotonic() - fetched > BLOCKHASH_MAX_AGE:
                        blockhash, fetched = finalized_blockhash(), time.monotonic()
                    ix = update_list_ix(pool, entries[start : start + n], start)
                    try:
                        sigs.append(send(signer.tx([ix], CU_BASE + CU_PER_VALIDATOR * n, blockhash)))
                    except (RpcError, OSError) as e:
                        log(f"  send [{start}..{start + n}) failed, retrying next round: {e}")
            else:
                tx = signer.tx(final_ixs(pool), CU_FINAL, finalized_blockhash())
                try:
                    sigs.append(send(tx))
                    log(f"  sent pool balance update {sigs[-1]}")
                except (RpcError, OSError) as e:
                    log(f"  pool balance update failed to send, retrying: {e}")
            if sigs:
                ok, failed = wait_for(sigs)
                log(f"  {len(sigs)} sent, {ok} confirmed, {failed} failed, "
                    f"{len(sigs) - ok - failed} unconfirmed")
        except (RpcError, OSError) as e:
            log(f"round {round_no}: RPC error, retrying: {e}")
        if time.monotonic() >= deadline:
            break
        time.sleep(3)
    print(f"ERROR: pool still stale after {BUDGET // 60} min ({round_no} rounds)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
