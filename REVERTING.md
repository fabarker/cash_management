# Reversing a fix

Each finding was fixed in its own commit, so any one can be undone without
disturbing the others. Two of them are config-driven and need no git at all.

## The recipe

```bash
git revert --no-commit <sha>
git checkout HEAD -- tests/     # keep the tests
git commit -m "Revert: <what and why>"
```

The `checkout` step matters. Every fix touched `tests/test_cash_optimizer.py`,
so reverting one there conflicts with the tests added by the others. Keeping
the tests and reverting only the source is deliberate: **the tests for the
reverted fix will then fail, and their names tell you exactly what behaviour
you gave up.** That is the point. Delete them once you have read them.

Verified: the source reverts cleanly for four of the five below. The
exception is noted.

---

## The five fixes

### `f0e3287` — Price manual trades at the rate they are dealt at (F22)

`CashManager` valued a foreign settlement at spot mid while the optimizer used
the tenor's bid or ask, so the same trade priced by the two paths differed by
the half-spread — always in the manual plan's favour.

- **Reverses cleanly.** Touches `cash_manager.py` only.
- **You lose:** `compare()` becomes an unfair comparison again, flattering
  whichever side was entered by hand.
- **No config alternative.**

### `0869356` — Scale the trade reporting floor by currency (F28)

Trades below a floor are omitted from the reported plan. The floor was a flat
`0.01` for every currency; it is now `0.01` of base, converted per currency.

- **Conflicts if reverted alone** — one hunk in `models.py`, because the
  `min_trade` block from `54b8a23` sits directly against it. Either resolve
  that hunk by hand, or revert `54b8a23` first and then this, which is clean.
- **You lose:** a flat threshold again, worth about 0.00005 of base in yen and
  0.008 in dollars.
- **Config alternative:** none for the scaling, but `trade_report_floor` sets
  the level.

### `54b8a23` — Add an optional minimum trade size (F15)

"Deal at least this much or not at all", enforced through the activation
binaries, which had never carried a floor or a cost.

- **Reverses cleanly.** Touches `models.py` and `optimizer.py`.
- **Config alternative — prefer this.** The default is `min_trade=0.0`, so the
  fix is inert unless switched on. There is nothing to revert unless you have
  set it; if you have, set it back to `0.0`.
- **You lose:** nothing, at the default.

### `c4d6e68` — Explain why a manual plan beat the optimizer (F23)

Replaces "This should not happen" with the list of rules the hand-entered plan
actually broke.

- **Reverses cleanly.** Touches `cash_manager.py` only.
- **You lose:** `CashManager.constraint_violations()`, and the message goes
  back to calling a routine outcome impossible.
- **No config alternative.**

### `1e494eb` — Monotonic drawdown on the holding, not the signed balance (F11)

The no-carry rule said an overdraft could only deepen and never be repaid. It
now applies to `bal_pos`.

- **Reverses cleanly.** Touches `optimizer.py` only.
- **You lose:** a currency left overdrawn after its last cash flow becomes
  unrecoverable again, so some scenarios return `Infeasible` that currently
  solve. This was one of the three constraints behind the day-0 funding trap.
- **No config alternative.**

---

## Checking a revert

```bash
python -m unittest discover -s tests -v
```

Expect failures in the class named for the finding you reverted — that is the
suite telling you what changed. Everything else should stay green. If anything
*else* fails, the revert has had an effect beyond its finding and is worth a
second look.
