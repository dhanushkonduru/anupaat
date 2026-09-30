"""Synthetic UPI fraud cases: blanket freeze vs traced freeze.

Generates seeded, reproducible fraud cases (victims, 3-4 mule layers across 3
banks, cash-outs, and innocent merchants/individuals who receive part of the
stolen money as ordinary payments), replays each case through the reference
tracer in every mode, and writes:

    results/summary.csv               one row per case
    results/aggregate.csv             totals across all cases
    results/innocent_money_frozen.png blanket vs traced chart

All data produced here is synthetic.

Run from the repo root:
    python -m sim.simulate --seed 42 --cases 500

Author: Dhanush (DK19)
"""

from __future__ import annotations

import argparse
import random
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from reference.tracer import MODES, Event, Tracer, cashout, fraud, income, transfer  # noqa: E402

BANKS = ("BankA", "BankB", "BankC")
RUPEE = 100  # paise
REPORT_ORDER = ("full_freeze", "proportional", "fifo")

# Share of a mule's balance cashed out at each layer depth (deeper = more).
CASHOUT_RANGE = [(0.00, 0.15), (0.10, 0.30), (0.20, 0.45), (0.35, 0.60)]


@dataclass
class Account:
    id: str
    bank: str
    kind: str  # "mule", "merchant" or "individual"
    is_innocent: bool  # ground truth for scoring only; the tracer never sees it


@dataclass
class Case:
    case_id: int
    n_layers: int
    victims: list[str]
    accounts: dict[str, Account] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)


class _Builder:
    """Emits events in time order and keeps a running balance per account,
    so every generated outflow is feasible."""

    def __init__(self, case: Case):
        self.case = case
        self.bal: defaultdict[str, int] = defaultdict(int)
        self.t = 0

    def _emit(self, make, *args) -> None:
        self.t += 1
        self.case.events.append(make(self.t, self.t, *args))

    def fraud(self, victim: str, to: str, amount: int) -> None:
        self._emit(fraud, victim, to, amount)
        self.bal[to] += amount

    def income(self, to: str, amount: int) -> None:
        self._emit(income, to, amount)
        self.bal[to] += amount

    def transfer(self, src: str, dst: str, amount: int) -> None:
        if amount <= 0:
            return
        self._emit(transfer, src, dst, amount)
        self.bal[src] -= amount
        self.bal[dst] += amount

    def cashout(self, src: str, amount: int) -> None:
        if amount <= 0:
            return
        self._emit(cashout, src, amount)
        self.bal[src] -= amount


def _rupees(rng: random.Random, lo: int, hi: int) -> int:
    return rng.randint(lo, hi) * RUPEE


def _whole_rupees(paise: int) -> int:
    return paise // RUPEE * RUPEE


def generate_case(case_id: int, rng: random.Random) -> Case:
    n_victims = rng.randint(1, 5)
    n_layers = rng.randint(3, 4)
    case = Case(case_id, n_layers, [f"C{case_id:03d}-V{i + 1}" for i in range(n_victims)])
    b = _Builder(case)

    def add(acc_id: str, kind: str, innocent: bool) -> str:
        case.accounts[acc_id] = Account(acc_id, rng.choice(BANKS), kind, innocent)
        return acc_id

    # Mule layers. Mules hold little money of their own before the fraud.
    layers: list[list[str]] = []
    for depth in range(n_layers):
        size = rng.randint(1, 3) if depth == 0 else rng.randint(2, 4)
        layers.append([add(f"C{case_id:03d}-M{depth + 1}{j + 1}", "mule", False) for j in range(size)])
    for m in (m for layer in layers for m in layer):
        if rng.random() < 0.6:
            b.income(m, _rupees(rng, 500, 5_000))

    # Innocent account holders with ordinary prior balances.
    innocents = []
    for j in range(rng.randint(2, 5)):
        kind = "merchant" if rng.random() < 0.6 else "individual"
        innocents.append(add(f"C{case_id:03d}-I{j + 1}", kind, True))
    for acc in innocents:
        b.income(acc, _rupees(rng, 20_000, 5_00_000))

    def everyday_activity() -> None:
        # Innocent accounts keep earning and spending as usual.
        for acc in innocents:
            if rng.random() < 0.5:
                b.income(acc, _rupees(rng, 1_000, 50_000))
            if rng.random() < 0.4:
                b.cashout(acc, _whole_rupees(int(b.bal[acc] * rng.uniform(0.05, 0.30))))

    # The frauds: each victim pays one or two first-layer mules.
    for v in case.victims:
        loss = _rupees(rng, 10_000, 2_00_000)
        targets = rng.sample(layers[0], k=min(len(layers[0]), rng.randint(1, 2)))
        first = _whole_rupees(loss * rng.randint(40, 70) // 100) if len(targets) == 2 else loss
        b.fraud(v, targets[0], first)
        if len(targets) == 2:
            b.fraud(v, targets[1], loss - first)

    for depth, layer in enumerate(layers):
        everyday_activity()
        lo, hi = CASHOUT_RANGE[min(depth, len(CASHOUT_RANGE) - 1)]
        for m in layer:
            b.cashout(m, _whole_rupees(int(b.bal[m] * rng.uniform(lo, hi))))

            # Some stolen money is spent at shops or paid to individuals.
            if rng.random() < 0.5:
                for acc in rng.sample(innocents, k=rng.randint(1, 2)):
                    cap = _whole_rupees(int(b.bal[m] * 0.3))
                    b.transfer(m, acc, min(_rupees(rng, 500, 15_000), cap))

            if depth == len(layers) - 1:
                continue  # last layer: whatever is left sits here when the freeze lands

            # Fan out to the next layer; fan-in happens when senders pick the same mule.
            nxt = layers[depth + 1]
            send = _whole_rupees(int(b.bal[m] * rng.uniform(0.85, 1.0)))
            receivers = rng.sample(nxt, k=rng.randint(1, min(3, len(nxt))))
            weights = [rng.randint(1, 10) for _ in receivers]
            sent = 0
            for i, (r, w) in enumerate(zip(receivers, weights)):
                amt = send - sent if i == len(receivers) - 1 else _whole_rupees(send * w // sum(weights))
                b.transfer(m, r, amt)
                sent += amt

    everyday_activity()
    return case


def evaluate(case: Case) -> dict:
    """Replay one case in every mode and score the result against ground truth."""
    innocents = [a for a in sorted(case.accounts) if case.accounts[a].is_innocent]
    row = {
        "case_id": case.case_id,
        "n_victims": len(case.victims),
        "n_layers": case.n_layers,
        "n_accounts": len(case.accounts),
        "n_innocent": len(innocents),
    }
    for mode in MODES:
        tr = Tracer(mode).run(case.events)
        tr.check_invariants()
        accs = tr.accounts()
        if mode == "proportional":
            row["total_loss_paise"] = sum(tr.losses.values())
            row["stolen_cashed_out_paise"] = sum(tr.cashed_out.values())
            row["victim_loss_paise"] = ";".join(str(tr.losses[v]) for v in case.victims)
            multi = [a for a in accs if len(tr.stolen(a)) > 1]
            row["multi_victim_accounts"] = len(multi)
            for a in multi:  # exercise allocation on every shared account
                split = tr.allocate(a, tr.lien(a) // 2)
                assert sum(split.values()) == tr.lien(a) // 2
                assert all(split[v] <= s for v, s in tr.stolen(a).items())
        stolen_in = {a: sum(tr.stolen(a).values()) for a in accs}
        row[f"{mode}_frozen_total_paise"] = sum(tr.lien(a) for a in accs)
        row[f"{mode}_stolen_frozen_paise"] = sum(stolen_in[a] for a in accs if tr.lien(a))
        row[f"{mode}_legit_frozen_all_paise"] = sum(tr.legit_frozen(a) for a in accs)
        row[f"{mode}_legit_frozen_innocent_paise"] = sum(tr.legit_frozen(a) for a in innocents)
        row[f"{mode}_stolen_frozen_innocent_paise"] = sum(stolen_in[a] for a in innocents if tr.lien(a))
        row[f"{mode}_innocent_frozen_any"] = sum(1 for a in innocents if tr.lien(a) > 0)
        row[f"{mode}_innocent_over_frozen"] = sum(1 for a in innocents if tr.lien(a) > stolen_in[a])
        row[f"{mode}_victim_recoverable_paise"] = ";".join(str(tr.recoverable(v)) for v in case.victims)
    return row


def run(seed: int, n_cases: int) -> pd.DataFrame:
    rows = []
    for cid in range(1, n_cases + 1):
        rng = random.Random(seed * 1_000_003 + cid)
        rows.append(evaluate(generate_case(cid, rng)))
    return pd.DataFrame(rows)


def aggregate(df: pd.DataFrame) -> pd.DataFrame:
    def victim_share(mode: str) -> float:
        rec = [int(x) for s in df[f"{mode}_victim_recoverable_paise"] for x in s.split(";")]
        loss = [int(x) for s in df["victim_loss_paise"] for x in s.split(";")]
        return round(100 * sum(r / l for r, l in zip(rec, loss)) / len(loss), 2)

    metrics = [
        ("Legitimate money frozen in innocent accounts (Rs)", "legit_frozen_innocent_paise", RUPEE),
        ("Innocent accounts with any freeze", "innocent_frozen_any", 1),
        ("Innocent accounts frozen beyond their stolen share", "innocent_over_frozen", 1),
        ("Legitimate money frozen, all accounts (Rs)", "legit_frozen_all_paise", RUPEE),
        ("Stolen money frozen (Rs)", "stolen_frozen_paise", RUPEE),
        ("Stolen money frozen in innocent accounts (Rs)", "stolen_frozen_innocent_paise", RUPEE),
        ("Total money frozen (Rs)", "frozen_total_paise", RUPEE),
    ]
    table = []
    for label, col, div in metrics:
        entry = {"metric": label}
        for mode in REPORT_ORDER:
            total = int(df[f"{mode}_{col}"].sum())
            entry[mode] = total / div if div != 1 else total
        table.append(entry)
    table.append({"metric": "Mean recoverable share per victim (%)", **{m: victim_share(m) for m in REPORT_ORDER}})
    return pd.DataFrame(table, dtype=object)


def plot(agg: pd.DataFrame, n_cases: int, seed: int, path: Path) -> None:
    get = agg.set_index("metric")
    stolen = get.loc["Stolen money frozen (Rs)"]
    innocent = get.loc["Legitimate money frozen in innocent accounts (Rs)"]
    mule_own = get.loc["Legitimate money frozen, all accounts (Rs)"] - innocent
    modes = ["full_freeze", "proportional"]
    labels = ["Blanket freeze\n(today)", "Traced freeze\n(Anupaat)"]
    crore = 1e7

    segments = [
        ("Stolen money", stolen, "#8a8f98", "white"),
        ("Legitimate money in innocent accounts", innocent, "#d1495b", "white"),
        ("Legitimate money in mule accounts", mule_own, "#edae49", None),
    ]

    fig, ax = plt.subplots(figsize=(9, 3.4), dpi=150)
    left = [0.0, 0.0]
    for name, series, color, text_color in segments:
        vals = [series[m] / crore for m in modes]
        ax.barh(labels, vals, left=left, color=color, label=name, height=0.6)
        for i, v in enumerate(vals):
            if text_color and v > 0:
                ax.text(left[i] + v / 2, i, f"Rs {v:,.2f} cr", va="center", ha="center",
                        fontsize=9, color=text_color, fontweight="bold")
        left = [l + v for l, v in zip(left, vals)]
    for i in range(len(modes)):
        ax.text(left[i] + max(left) * 0.01, i, f"total Rs {left[i]:,.2f} cr", va="center", fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, max(left) * 1.2)
    ax.set_xlabel("Money frozen, summed over all cases (Rs crore)")
    ax.set_title(f"Money frozen: blanket vs traced freeze, {n_cases} synthetic cases (seed {seed})",
                 fontsize=11, loc="left")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.28), ncol=3, frameon=False, fontsize=8.5)
    fig.text(0.01, -0.02, "Synthetic data. Both freezes lock the same stolen money; "
             "traced freeze uses proportional tracing.", fontsize=7.5, color="#555555")
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--cases", type=int, default=500)
    parser.add_argument("--out", type=Path, default=Path("results"))
    args = parser.parse_args()

    df = run(args.seed, args.cases)
    agg = aggregate(df)
    args.out.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out / "summary.csv", index=False)
    agg.to_csv(args.out / "aggregate.csv", index=False)
    plot(agg, args.cases, args.seed, args.out / "innocent_money_frozen.png")

    print(f"{args.cases} synthetic cases, seed {args.seed}")
    print(f"victims: {int(df.n_victims.sum())}, accounts: {int(df.n_accounts.sum())}, "
          f"innocent accounts: {int(df.n_innocent.sum())}, "
          f"multi-victim accounts: {int(df.multi_victim_accounts.sum())}")
    print(f"total stolen: Rs {df.total_loss_paise.sum() / RUPEE:,.2f}, "
          f"cashed out (proportional trace): Rs {df.stolen_cashed_out_paise.sum() / RUPEE:,.2f}")
    with pd.option_context("display.width", 140, "display.max_colwidth", 60,
                           "display.float_format", "{:,.2f}".format):
        print(agg.to_string(index=False))


if __name__ == "__main__":
    main()
