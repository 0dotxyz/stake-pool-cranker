#!/usr/bin/env python3
"""Fail (exit 1) if the stake pool is stale or the fee payer is running low.

Runs after every crank. A red workflow run is the alert.
Reads: STAKE_POOL, RPC_URL, FEE_PAYER_KEYPAIR, MIN_FEE_PAYER_SOL from the environment.
"""
import base64
import json
import os
import sys
import time
import urllib.request

RPC = os.environ["RPC_URL"]
POOL = os.environ.get("STAKE_POOL", "DqhH94PjkZsjAqEze2BEkWhFQJ6EyU6MdtMphMgnXqeK")
MIN_SOL = float(os.environ.get("MIN_FEE_PAYER_SOL", "0.2"))
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
# The CLI confirms its transactions at "confirmed"; reading at the default "finalized"
# lags ~15s behind and reports a pool as stale right after a successful crank.
COMMITMENT = {"commitment": "confirmed"}


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    if "error" in out:
        raise SystemExit(f"rpc {method}: {out['error']}")
    return out["result"]


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    s = ""
    while n:
        n, rem = divmod(n, 58)
        s = ALPHABET[rem] + s
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + s


def fee_payer_pubkey() -> str:
    # keypair json is a 64-byte array; the public key is the last 32 bytes
    kp = json.loads(os.environ["FEE_PAYER_KEYPAIR"])
    return b58encode(bytes(kp[32:]))


def pool_epochs():
    epoch = rpc("getEpochInfo", [COMMITMENT])["epoch"]
    acct = rpc("getAccountInfo", [POOL, {"encoding": "base64", **COMMITMENT}])["value"]
    data = base64.b64decode(acct["data"][0])
    # StakePool layout: u8 account_type, 3 pubkeys, u8 bump, 5 pubkeys, u64 total_lamports,
    # u64 pool_token_supply, u64 last_update_epoch
    off = 1 + 32 * 3 + 1 + 32 * 5 + 8 + 8
    return int.from_bytes(data[off : off + 8], "little"), epoch


def main() -> int:
    problems = []

    last_update_epoch, epoch = pool_epochs()
    if last_update_epoch != epoch:
        # one more look after a pause, in case the final crank tx is still landing
        time.sleep(20)
        last_update_epoch, epoch = pool_epochs()
    if last_update_epoch != epoch:
        problems.append(f"pool is STALE: last_update_epoch={last_update_epoch}, current epoch={epoch}")
    else:
        print(f"pool is current (epoch {epoch})")

    payer = fee_payer_pubkey()
    lamports = rpc("getBalance", [payer, COMMITMENT])["value"]
    sol = lamports / 1e9
    if sol < MIN_SOL:
        problems.append(f"fee payer {payer} is LOW: {sol:.4f} SOL (min {MIN_SOL})")
    else:
        print(f"fee payer {payer} has {sol:.4f} SOL")

    for p in problems:
        print("ERROR:", p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
