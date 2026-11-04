# stake-pool-cranker

Keeps the LST stake pool (`DqhH94PjkZsjAqEze2BEkWhFQJ6EyU6MdtMphMgnXqeK`) updated every epoch.

A GitHub Actions cron runs `crank.py` every hour. It does nothing when the pool is already
current, so only one real crank happens per epoch (~185 transactions, ~0.003 SOL with the
default priority fee). After each run, `check.py` fails the workflow if the pool is stale or
the fee payer is running low, so a red run is the alert.

`crank.py` does the same as `spl-stake-pool update --stale-only`, but packs 11 validators per
transaction instead of 5 and copes with a flaky RPC (see Gotchas). Its only dependency is
`solders` for signing.

## Setup (once)

1. Create a fresh fee payer. It needs no authority over the pool, cranking is permissionless.

   ```bash
   solana-keygen new --outfile fee-payer.json --no-bip39-passphrase
   solana-keygen pubkey fee-payer.json
   ```

2. Send it ~2 SOL. That covers roughly a year.

3. Add two repository secrets (Settings → Secrets and variables → Actions):

   | Secret | Value |
   |---|---|
   | `FEE_PAYER_KEYPAIR` | the full contents of `fee-payer.json` (the `[12,34,...]` array) |
   | `RPC_URL` | a paid mainnet RPC URL. The public endpoint rate-limits a full crank. |

4. Delete `fee-payer.json` from your machine after it's in 1Password. Never commit it.

5. Run the workflow once by hand (Actions → crank → Run workflow).

## Tuning (optional repository variables)

| Variable | Default | Meaning |
|---|---|---|
| `COMPUTE_UNIT_PRICE` | `50000` | priority fee in microlamports per compute unit |
| `MIN_FEE_PAYER_SOL` | `0.2` | `check.py` fails below this balance |

Expected cost: ~0.003 SOL per epoch (~185 transactions), so roughly 0.05 SOL per month.
A 0.5 SOL fee payer lasts most of a year; the default threshold gives ~4 months of warning.

## Alerts

A red run emails the repo owner by default. For Slack, add a repository secret
`SLACK_WEBHOOK_URL` (Slack app → Incoming Webhooks); failed runs then post a message with a
link to the run. No secret, no message.

## Gotchas

- GitHub disables scheduled workflows on repos with no commits for 60 days. Push something
  occasionally, or re-enable it under Actions when you get the notice.
- The pool has ~2000 validators. The `spl-stake-pool` CLI updates 5 per transaction (~405 txs),
  aborts on the first RPC error, and checks every transaction's fee with `getFeeForMessage`
  first. When the RPC load-balances across out-of-sync nodes, that call returns null for a
  blockhash the same URL just handed out, and the CLI dies with `Custom: Invalid blockhash`
  (epoch 1077, 2026-11-04: four runs in a row). `crank.py` avoids all of that: it never calls
  `getFeeForMessage`, signs with a *finalized* blockhash every node already knows, and treats
  each transaction on its own: anything that fails to send or land is rebuilt next round from
  the on-chain validator list. It keeps going for up to 22 minutes, and the next hourly run
  resumes from wherever it stopped.
- The program silently skips a validator whose stake accounts are passed wrong, rather than
  failing. To check `crank.py` against mainnet without sending anything (simulation only; any
  RPC works, the public one included):

  ```bash
  pip install -r requirements.txt
  RPC_URL=https://api.mainnet-beta.solana.com DRY_RUN=1 \
    FEE_PAYER_PUBKEY=3BbRk7YBy9mSTF7J39aHGteZsyeZ1AzEwHP5jbUPN1DW python3 crank.py
  ```

  Every validator line should end in `ok`; the last line, the pool balance update, fails with
  `Custom: 16` (validators still stale) until they are all updated. It only has something to
  simulate while the pool is stale, i.e. early in an epoch.
- Manual crank from a laptop, if ever needed: `RPC_URL=... FEE_PAYER_KEYPAIR="$(cat fee-payer.json)" python3 crank.py`,
  or with the upstream CLI as a fallback:

  ```bash
  spl-stake-pool --url "$RPC_URL" \
    --fee-payer fee-payer.json --staker fee-payer.json --manager fee-payer.json --token-owner fee-payer.json \
    --with-compute-unit-price 50000 \
    update DqhH94PjkZsjAqEze2BEkWhFQJ6EyU6MdtMphMgnXqeK --stale-only
  ```

  The extra signer flags are needed because the CLI opens all four at startup; `update` only
  ever signs with the fee payer.
