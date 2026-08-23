"""Generate the scenario library.

Run from the repository root:  python3 scenarios/generate.py

FX forwards are derived from covered interest parity rather than invented,
so the points always carry the right sign against the rate differential:
a currency yielding more than the base trades at a forward discount.  Over
one and two days the points are small relative to the dealing spread, which
is itself realistic and is why the tenor choice in these scenarios is
usually driven by carry rather than by the forward.
"""
import json
import pathlib

# ── Market data ───────────────────────────────────────────────

# USD per one unit, the only place a rate is written down.  Every pair is
# crossed from here, so the book is arbitrage-free by construction.
USD_PER = {
    "USD": 1.0,
    "GBP": 1.2658,
    "EUR": 1.0800,
    "JPY": 1.0 / 152.0,
    "CHF": 1.0 / 0.88,
}

# Money-market rate used for the forward, then the commercial spread either
# side of it for what a corporate account actually earns and pays.
POLICY_PA = {"USD": 4.25, "GBP": 4.00, "EUR": 2.15, "JPY": 0.50, "CHF": 0.25}
CREDIT_PA = {"USD": 3.75, "GBP": 3.50, "EUR": 1.65, "JPY": 0.10, "CHF": 0.05}
DEBIT_PA  = {"USD": 6.25, "GBP": 6.00, "EUR": 4.15, "JPY": 2.00, "CHF": 2.25}
BASIS     = {"USD": 360, "GBP": 365, "EUR": 360, "JPY": 365, "CHF": 360}

TENORS = {"T0": 0, "T1": 1, "T2": 2}

# Half-spread as a fraction of the rate.
SPREADS = {"tight": 0.00010, "normal": 0.00025, "wide": 0.00080}

COMMISSION = {
    "institutional": [(1_000_000, 5.0), (500_000_000, 2.0)],
    "standard":      [(500_000, 20.0), (500_000_000, 10.0)],
    "three_tier":    [(250_000, 35.0), (2_000_000, 18.0), (500_000_000, 8.0)],
    "flat":          [(500_000_000, 12.0)],
    "retail":        [(100_000, 60.0), (1_000_000, 35.0), (500_000_000, 20.0)],
}


def forward(base: str, foreign: str, lag: int) -> float:
    """Base per one foreign at *lag* days, by covered interest parity."""
    spot = USD_PER[foreign] / USD_PER[base]
    if lag == 0:
        return spot
    gb = 1.0 + POLICY_PA[base] / 100.0 * lag / BASIS[base]
    gf = 1.0 + POLICY_PA[foreign] / 100.0 * lag / BASIS[foreign]
    return spot * gb / gf


def quotes(base: str, foreign: list, spread: str) -> dict:
    hs = SPREADS[spread]
    out = {}
    for ccy in foreign:
        legs = {}
        for tenor, lag in TENORS.items():
            mid = forward(base, ccy, lag)
            legs[tenor] = {"bid": round(mid * (1 - hs), 10),
                           "ask": round(mid * (1 + hs), 10)}
        out[ccy] = legs
    return out


def scenario(sid, name, narrative, base, foreign, horizon, opening, flows,
             *, spread="normal", commission="standard", expectation="",
             min_trade=0.0, extra=None):
    ccys = [base] + [c for c in foreign if c != base]
    s = {
        "id": sid,
        "name": name,
        "narrative": narrative,
        "expectation": expectation,
        "base_ccy": base,
        "currencies": ccys,
        "horizon_days": horizon,
        "tenors": TENORS,
        "spread_profile": spread,
        "commission_profile": commission,
        "commission_tiers": [{"threshold": t, "rate_bps": r}
                             for t, r in COMMISSION[commission]],
        "day_count_basis": {c: BASIS[c] for c in ccys},
        "credit_carry_pa": {c: CREDIT_PA[c] for c in ccys},
        "debit_carry_pa": {c: DEBIT_PA[c] for c in ccys},
        "fx_quotes": quotes(base, [c for c in ccys if c != base], spread),
        "opening_balances": opening,
        "cash_flows": [{"ccy": c, "day": d, "amount": a} for c, d, a in flows],
        "min_trade": min_trade,
        "max_trade": COMMISSION[commission][-1][0],
    }
    if extra:
        s.update(extra)
    return s


SCENARIOS = [
    scenario(
        "S01", "Single dollar payment",
        "A sterling book with one dollar invoice falling due mid-week and ample "
        "cash to meet it. The plainest case there is.",
        "GBP", ["USD"], 7,
        {"GBP": 4_000_000.0, "USD": 0.0},
        [("USD", 4, -750_000.0)],
        expectation="One USD buy landing on day 4. Nothing held before day 2, "
                    "because the payment is out of dealing reach until then.",
    ),
    scenario(
        "S02", "Staggered payables",
        "Three foreign invoices on different dates. The optimiser has to fund "
        "each one separately rather than pre-buying a single block.",
        "USD", ["GBP", "EUR", "CHF"], 8,
        {"USD": 8_000_000.0},
        [("GBP", 3, -700_000.0), ("EUR", 5, -1_100_000.0), ("CHF", 6, -400_000.0)],
        commission="institutional", spread="tight",
        expectation="Three buys, each inside its own settlement window. "
                    "Institutional commission makes the tenor choice cheap.",
    ),
    scenario(
        "S03", "Opening dollar overdraft",
        "The account is already short dollars at the start of the week and "
        "nothing else is scheduled in that currency. It has to be cured.",
        "GBP", ["USD"], 6,
        {"GBP": 3_000_000.0, "USD": -620_000.0},
        [],
        expectation="A USD buy clearing the overdraft inside the settlement "
                    "window. This case used to return Infeasible.",
    ),
    scenario(
        "S04", "Receipt funds a later payment",
        "A dollar receipt lands on Tuesday and a dollar payment falls due on "
        "Friday. The receipt should simply be kept to meet it.",
        "GBP", ["USD"], 8,
        {"GBP": 2_000_000.0},
        [("USD", 1, 1_400_000.0), ("USD", 5, -950_000.0)],
        expectation="No round trip. The earmarked 950k is held; the 450k "
                    "surplus is swept back to sterling.",
    ),
    scenario(
        "S05", "Bridge across a timing gap",
        "The euro payment lands three days before the euro receipt that was "
        "meant to cover it. A genuine funding gap, not a shortfall.",
        "GBP", ["EUR"], 8,
        {"GBP": 3_500_000.0},
        [("EUR", 2, -1_600_000.0), ("EUR", 5, 1_750_000.0)],
        expectation="Either a euro purchase for day 2 or a priced overdraft "
                    "across the gap, whichever the commission makes cheaper.",
    ),
    scenario(
        "S06", "Thin base balance",
        "Payables are covered, but only just. Very little sterling headroom, "
        "so the overdraft rate on the base account starts to matter.",
        "EUR", ["USD", "CHF"], 7,
        {"EUR": 210_000.0, "USD": 0.0, "CHF": 240_000.0},
        [("USD", 3, -280_000.0), ("CHF", 4, -160_000.0)],
        commission="three_tier",
        expectation="The Swiss holding is used before anything is bought. "
                    "Watch the base balance for a shallow overdraft.",
    ),
    scenario(
        "S07", "Dollar carry temptation",
        "The dollar is paying well above sterling and the account has a large "
        "unearmarked dollar credit. The obligation is trivially small.",
        "GBP", ["USD"], 8,
        {"GBP": 5_000_000.0, "USD": 2_500_000.0},
        [("USD", 6, -50_000.0)],
        spread="tight", commission="institutional",
        extra={"credit_carry_pa_override": {"USD": 6.40},
               "note": "A dollar rate above sterling is an ordinary enough "
                       "state of the world and is what makes this a test."},
        expectation="The dollar credit must be swept promptly despite the "
                    "yield. Toggle the speculative-holding limit off and the "
                    "cost should fall -- that fall is the carry being taken.",
    ),
    scenario(
        "S08", "Yen invoice, large notional",
        "A single large yen payable. Nothing unusual economically, but the "
        "notional is two orders of magnitude larger than the others.",
        "USD", ["JPY"], 7,
        {"USD": 5_000_000.0},
        [("JPY", 4, -420_000_000.0)],
        commission="flat",
        expectation="One yen buy of 420m. A flat commission removes the tier "
                    "structure, so only the spread and carry drive the tenor.",
    ),
    scenario(
        "S09", "Payment due today",
        "A dollar payment falls due on day zero, before any forward trade "
        "could settle against it.",
        "GBP", ["USD"], 6,
        {"GBP": 2_800_000.0},
        [("USD", 0, -540_000.0)],
        expectation="Same-day settlement or a short dollar overdraft on day "
                    "zero, whichever prices better.",
    ),
    scenario(
        "S10", "Receipt on the closing day",
        "A euro receipt arrives on the last day of the ladder and must still "
        "be back in base by the close.",
        "CHF", ["EUR"], 7,
        {"CHF": 1_400_000.0},
        [("EUR", 6, 880_000.0)],
        expectation="The receipt is sold forward to settle the day it lands, "
                    "so it is never actually held.",
    ),
    scenario(
        "S11", "Full four-currency book",
        "Everything at once: payables and receivables across all four foreign "
        "currencies, with one of them opening overdrawn.",
        "GBP", ["USD", "EUR", "JPY", "CHF"], 9,
        {"GBP": 8_000_000.0, "USD": -300_000.0, "EUR": 500_000.0,
         "JPY": 0.0, "CHF": 0.0},
        [("USD", 3, -1_200_000.0), ("EUR", 4, -800_000.0),
         ("JPY", 5, -180_000_000.0), ("CHF", 6, -650_000.0),
         ("USD", 7, 700_000.0), ("CHF", 8, 200_000.0)],
        commission="three_tier",
        expectation="The euro opening credit offsets part of the euro payable. "
                    "The dollar overdraft is cured before the dollar payable.",
    ),
    scenario(
        "S12", "Dollar-based treasury",
        "The same problem seen from a US book: sterling and euro payables "
        "against a dollar base.",
        "USD", ["GBP", "EUR"], 7,
        {"USD": 5_000_000.0},
        [("GBP", 3, -900_000.0), ("EUR", 5, -1_400_000.0)],
        expectation="Both foreign legs now yield less than base, so there is "
                    "no incentive to hold either a day longer than needed.",
    ),
    scenario(
        "S13", "Euro-based treasury",
        "A European book funding dollar and Swiss obligations, with the euro "
        "paying materially less than the dollar.",
        "EUR", ["USD", "GBP", "CHF"], 8,
        {"EUR": 6_000_000.0, "USD": 0.0, "CHF": 150_000.0},
        [("USD", 2, -1_800_000.0), ("CHF", 5, -900_000.0), ("GBP", 6, -450_000.0)],
        commission="institutional", spread="tight",
        expectation="Base yields less than the dollar here, so the limit on "
                    "speculative holdings is doing visible work.",
    ),
    scenario(
        "S14", "Yen-based treasury",
        "A Japanese book. Base notionals run to hundreds of millions and the "
        "base currency yields almost nothing.",
        "JPY", ["USD", "EUR"], 7,
        {"JPY": 900_000_000.0},
        [("USD", 3, -2_200_000.0), ("EUR", 5, -1_500_000.0)],
        commission="institutional",
        expectation="Every foreign currency out-yields the yen base, which is "
                    "the strongest carry pull in the library.",
    ),
    scenario(
        "S15", "Swiss-based treasury",
        "A Swiss book with the lowest base yield of the five and a euro "
        "receivable arriving mid-week.",
        "CHF", ["EUR", "USD"], 8,
        {"CHF": 3_200_000.0, "EUR": -220_000.0},
        [("EUR", 4, 1_100_000.0), ("USD", 5, -1_300_000.0)],
        commission="three_tier",
        expectation="The euro overdraft is cured, then the euro receipt covers "
                    "it and the surplus is swept.",
    ),
    scenario(
        "S16", "Need below the minimum ticket",
        "A trivial dollar payable against a dealing desk that will not quote "
        "below half a million.",
        "GBP", ["USD"], 6,
        {"GBP": 2_000_000.0},
        [("USD", 3, -12_000.0)],
        min_trade=500_000.0, commission="institutional",
        expectation="No legal plan exists. Expect NO FEASIBLE PLAN -- the "
                    "honest answer, not a defect.",
    ),
    scenario(
        "S17", "Straddling a commission tier",
        "The dollar payable sits just above the first commission band, so the "
        "cheap tier must be filled before the expensive one is used.",
        "EUR", ["USD"], 7,
        {"EUR": 3_000_000.0},
        [("USD", 4, -1_150_000.0)],
        commission="retail", spread="wide",
        expectation="A steep retail schedule and a wide dealing spread, "
                    "with the need crossing two bands. Check the commission "
                    "and spread lines in the breakdown.",
    ),
    scenario(
        "S18", "Several currencies overdrawn",
        "Three foreign accounts start the week short and nothing arrives to "
        "help. Everything has to be funded from base.",
        "USD", ["GBP", "EUR", "CHF"], 7,
        {"USD": 6_000_000.0, "GBP": -600_000.0, "EUR": -650_000.0,
         "CHF": -300_000.0},
        [],
        commission="standard",
        expectation="Three buys, each sized to its own overdraft, all inside "
                    "the settlement window.",
    ),
    scenario(
        "S19", "Alternating dollar flows",
        "Pay, receive, pay again in one currency across a single week. The "
        "deepest point of the ladder is what has to be funded, not the net.",
        "JPY", ["USD"], 8,
        {"JPY": 400_000_000.0},
        [("USD", 1, -700_000.0), ("USD", 3, 1_100_000.0), ("USD", 6, -900_000.0)],
        expectation="Netting the week to a single figure would understate the "
                    "day-1 gap. Two separate fundings, or one plus an "
                    "overdraft.",
    ),
    scenario(
        "S20", "Genuinely insufficient funds",
        "Obligations exceed everything the account holds, however the trades "
        "are arranged. A financing question, not a constraint conflict.",
        "GBP", ["USD", "EUR"], 7,
        {"GBP": 250_000.0, "USD": 0.0, "EUR": 0.0},
        [("USD", 3, -2_400_000.0), ("EUR", 5, -1_800_000.0)],
        spread="wide",
        expectation="A plan is still returned, with INSUFFICIENT FUNDS set "
                    "and a negative terminal base equivalent showing how much "
                    "is short and when.",
    ),
]


# Why the expected outcome is the cheapest one.  Grounded in the figures the
# model actually produces, not in general principle: the commission schedule
# dominates almost every scenario here, and saying so is more use than a
# description of the cost model.
COST_REASONING = {
    "S01":
        "Pure dealing cost: 20bps on the first 500,000 and 10bps above it, "
        "plus the half-spread — about 1,136 all in. Nothing accrues either "
        "way because the dollars land on the day they are paid away. Buying "
        "earlier would add exposure at 1bp/day and buy nothing.",
    "S02":
        "Institutional pricing of 5bps then 2bps makes dealing cheap enough "
        "that there is no reason to bundle the three legs. Each is funded in "
        "its own settlement window: pre-buying would earn foreign credit "
        "below the dollar base rate and pay 1bp/day of exposure for nothing.",
    "S03":
        "The overdraft runs at 1.39bps/day against 20bps to clear it, so on "
        "carry alone it would take about a fortnight of overdraft to justify "
        "the trade. The horizon is six days — the terminal sweep is what "
        "forces the deal, and the only real choice left is the tenor.",
    "S04":
        "Holding the earmarked 950,000 is cheap: the dollar pays 3.75% "
        "against sterling's 3.50%, so the differential earns about 28, "
        "against 150 of exposure at 1bp/day. Selling and rebuying would cost "
        "20bps twice, near 1,500. Keeping it is obvious; only the surplus is "
        "swept.",
    "S05":
        "Funding the 1.6m euro gap outright costs roughly 2,700 in commission "
        "plus spread. Running the overdraft instead costs 1.15bps/day, about "
        "472 across the gap. The overdraft wins, and only the residual left "
        "after the receipt is dealt.",
    "S06":
        "With base thin, the Swiss balance is worth more used than swept — "
        "spending it avoids a 35bps first-tier commission on the same money "
        "twice over. What is left is a shallow euro overdraft, which is "
        "cheaper per day than dealing again.",
    "S07":
        "The dollar pays 6.40% against sterling's 3.50%, so 2.5m dollars "
        "earn about 0.82bps/day — 336 across the ladder. That is the "
        "temptation. Sweeping costs 624 of commission and 161 of spread. "
        "Turn the holding limit off and cost falls by around 158: the carry "
        "collected, net of the exposure charge. That fall is the speculation.",
    "S08":
        "A flat 12bps removes the tier structure, so the schedule no longer "
        "decides anything and the tenor is chosen on carry and spread alone. "
        "The yen yields almost nothing, so there is no reason to hold it a "
        "day longer than the payment requires.",
    "S09":
        "There is no time for carry to accrue and no tenor to choose beyond "
        "same-day, so the entire cost is the deal itself — 20bps on the "
        "first 500,000, 10bps on the remainder, plus the half-spread.",
    "S10":
        "Selling the receipt forward to settle the day it lands means it is "
        "never held, so it accrues neither carry nor exposure and the cost "
        "is one commission plus the half-spread. Holding it would earn the "
        "euro/franc differential, but the sweep forbids finishing long.",
    "S11":
        "Four currencies, one schedule. The euro opening credit is spent "
        "rather than swept because using it avoids paying 20bps on the same "
        "money twice. The dollar overdraft is cured early because 1.39bps/day "
        "compounds across the whole ladder while the commission is paid once.",
    "S12":
        "Sterling and the euro both yield less than the dollar base, so there "
        "is no carry reason to hold either. Every leg is funded as late as "
        "the settlement window allows, and the cost is commission and spread "
        "almost in full.",
    "S13":
        "The euro base yields 1.65% against the dollar's 3.75%, so holding "
        "dollars pays. Institutional pricing at 5bps makes the sweep cheap "
        "enough to do anyway — this is the scenario where the holding limit "
        "and the commission schedule pull hardest against each other.",
    "S14":
        "Every foreign currency out-yields the yen, so on carry alone the "
        "book would rather sit in dollars and euros. Base figures are 152 "
        "times larger, which scales commission and spread together and "
        "leaves the trade-off unchanged — ratios decide, not magnitudes.",
    "S15":
        "The franc has the lowest yield of the five, so holding the euro "
        "receipt pays a positive differential. The three-tier schedule still "
        "charges 35bps on the first 250,000, which is what stops the "
        "optimiser dealing more often than it needs to.",
    "S16":
        "No cost at all, because no legal plan exists. 12,000 dollars are "
        "needed and the desk will not quote below 500,000; dealing the "
        "minimum would leave 488,000 unwanted dollars, which the holding "
        "limit forbids. Infeasible is the correct answer, not a defect.",
    "S17":
        "Retail pricing dominates everything else: 60bps on the first "
        "100,000, 35bps to a million and 20bps above brings commission to "
        "about 3,750 on a 1.15m payment — roughly 33bps blended. The wide "
        "spread adds 850 more. Carry and exposure net to under 20 between "
        "them, so the schedule alone decides the answer.",
    "S18":
        "Three overdrafts, each cured once. Clearing them costs 20bps a "
        "time; leaving them costs between 1.25 and 1.39bps/day. Over a "
        "seven-day ladder the overdrafts are cheaper on carry, so it is the "
        "terminal sweep rather than the economics that forces the trades.",
    "S19":
        "Netting the week to −500,000 would suggest one late trade. The "
        "day-1 gap is 700,000, and funding all of it costs 20bps. Funding "
        "500,000 and running a 200,000 overdraft for two days costs "
        "1.74bps/day on the shortfall instead — which is why the trade is "
        "500,000 and not 700,000.",
    "S20":
        "Both payments are still made, and sterling carries the deficit: "
        "9.7m overdrawn balance-days at 1.64bps/day, about 1,590. That the "
        "plan looks affordable in commission terms is beside the point — the "
        "terminal base equivalent is −3.2m, and that is the real answer.",
}

for _sc in SCENARIOS:
    _sc["cost_reasoning"] = COST_REASONING[_sc["id"]]


def main() -> None:
    out = {
        "version": 1,
        "description": (
            "Twenty cash-management scenarios for the optimiser. FX forwards "
            "are derived from covered interest parity off a single USD cross "
            "table, so the book is arbitrage-free and the forward points "
            "carry the right sign against each rate differential."
        ),
        "market_data": {
            "usd_per_unit": USD_PER,
            "policy_pa": POLICY_PA,
            "credit_pa": CREDIT_PA,
            "debit_pa": DEBIT_PA,
            "day_count_basis": BASIS,
            "half_spreads": SPREADS,
            "commission_profiles": {
                k: [{"threshold": t, "rate_bps": r} for t, r in v]
                for k, v in COMMISSION.items()
            },
        },
        "scenarios": SCENARIOS,
    }
    path = pathlib.Path(__file__).resolve().parent / "cash_scenarios.json"
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path} ({len(SCENARIOS)} scenarios)")


if __name__ == "__main__":
    main()
