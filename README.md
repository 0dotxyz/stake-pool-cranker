# stake-pool-cranker

Keeps the LST stake pool (`DqhH94PjkZsjAqEze2BEkWhFQJ6EyU6MdtMphMgnXqeK`) updated every epoch.

A GitHub Actions cron runs `spl-stake-pool update` every hour. The command does nothing
when the pool is already current, so only one real crank happens per epoch (~400 transactions,
~0.005 SOL with the default priority fee). After each run, `check.py` fails the workflow if the
pool is stale or the fee payer is running low, so a red run is the alert.

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
   | `RPC_URL` | a paid mainnet RPC URL. The public endpoint rate-limits a 400-tx crank. |

4. Delete `fee-payer.json` from your machine after it's in 1Password. Never commit it.

5. Run the workflow once by hand (Actions → crank → Run workflow). The first run builds the
   CLI (~8 min); later runs restore it from cache in seconds.

## Tuning (optional repository variables)

| Variable | Default | Meaning |
|---|---|---|
| `COMPUTE_UNIT_PRICE` | `50000` | priority fee in microlamports per compute unit |
| `MIN_FEE_PAYER_SOL` | `0.2` | `check.py` fails below this balance |

## Gotchas

- GitHub disables scheduled workflows on repos with no commits for 60 days. Push something
  occasionally, or re-enable it under Actions when you get the notice.
- If a crank is interrupted mid-way, the next run finishes it: `--stale-only` skips validators
  already updated this epoch.
- Manual crank from a laptop, if ever needed:

  ```bash
  spl-stake-pool --url "$RPC_URL" \
    --fee-payer fee-payer.json --staker fee-payer.json --manager fee-payer.json --token-owner fee-payer.json \
    --with-compute-unit-price 50000 \
    update DqhH94PjkZsjAqEze2BEkWhFQJ6EyU6MdtMphMgnXqeK --stale-only
  ```

  The extra signer flags are needed because the CLI opens all four at startup; `update` only
  ever signs with the fee payer.
