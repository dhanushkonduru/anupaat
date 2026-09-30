"""Reference implementation of Anupaat's tracing, lien and allocation logic.

This is the plain-Python version of the logic planned for the DRUNIX chaincode.
The chaincode port must reproduce its output to the paisa, so the rules here
are strict:

- All money is integer paise. No floats anywhere.
- Events are applied in (timestamp, event id) order.
- Every split uses the largest-remainder method with a fixed tie-break order,
  so totals are conserved exactly.
- Dicts are always walked in sorted key order. No randomness, no clock.

Author: Dhanush (DK19)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

MODES = ("proportional", "fifo", "full_freeze")
KINDS = ("FRAUD", "TRANSFER", "INCOME", "CASHOUT")

# Source tag for an account's own (legitimate) money. Victim ids are strings.
OWN = None


@dataclass(frozen=True)
class Event:
    """One money movement.

    FRAUD     victim's money lands in the first mule account (dst)
    TRANSFER  src -> dst, both tracked accounts
    INCOME    legitimate money enters dst
    CASHOUT   money leaves src for an untracked destination (unrecoverable)
    """

    ts: int
    eid: int
    kind: str
    amount: int
    src: str | None = None
    dst: str | None = None
    victim: str | None = None

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"unknown event kind {self.kind!r}")
        if not isinstance(self.amount, int) or isinstance(self.amount, bool):
            raise TypeError("amount must be an int number of paise")
        if self.amount <= 0:
            raise ValueError("amount must be positive")
        needs = {
            "FRAUD": ("victim", "dst"),
            "TRANSFER": ("src", "dst"),
            "INCOME": ("dst",),
            "CASHOUT": ("src",),
        }[self.kind]
        for name in needs:
            if getattr(self, name) is None:
                raise ValueError(f"{self.kind} event needs {name}")
        if self.kind == "TRANSFER" and self.src == self.dst:
            raise ValueError("TRANSFER src and dst must differ")


def fraud(ts: int, eid: int, victim: str, to: str, amount: int) -> Event:
    return Event(ts, eid, "FRAUD", amount, dst=to, victim=victim)


def transfer(ts: int, eid: int, src: str, dst: str, amount: int) -> Event:
    return Event(ts, eid, "TRANSFER", amount, src=src, dst=dst)


def income(ts: int, eid: int, to: str, amount: int) -> Event:
    return Event(ts, eid, "INCOME", amount, dst=to)


def cashout(ts: int, eid: int, src: str, amount: int) -> Event:
    return Event(ts, eid, "CASHOUT", amount, src=src)


def split_largest_remainder(total: int, weights: list[int]) -> list[int]:
    """Split `total` in proportion to `weights`, in integers, conserving the sum.

    Each part gets floor(total * w / sum(w)). The paise left over go one each to
    the parts with the largest remainders; ties go to the earlier position, so
    the caller controls the tie-break by the order of `weights`.
    Requires 0 <= total <= sum(weights), which guarantees part_i <= weight_i.
    """
    denom = sum(weights)
    if any(w < 0 for w in weights):
        raise ValueError("weights must be non-negative")
    if total < 0 or total > denom:
        raise ValueError(f"cannot split {total} over weights summing to {denom}")
    if total == 0:
        return [0] * len(weights)
    parts = [total * w // denom for w in weights]
    rems = [total * w % denom for w in weights]
    leftover = total - sum(parts)
    order = sorted(range(len(weights)), key=lambda i: (-rems[i], i))
    for i in order[:leftover]:
        parts[i] += 1
    return parts


def allocate(stolen: dict[str, int], recovered: int) -> dict[str, int]:
    """Split `recovered` paise among victims in proportion to their traced share.

    Largest-remainder rounding; ties broken by sorted victim id. The result sums
    to `recovered` exactly and no victim gets more than their traced share.
    """
    victims = sorted(v for v, amt in stolen.items() if amt > 0)
    if recovered > sum(stolen[v] for v in victims):
        raise ValueError("recovered amount exceeds the traced stolen money")
    parts = split_largest_remainder(recovered, [stolen[v] for v in victims])
    return dict(zip(victims, parts))


class Tracer:
    """Replays events and tracks, per account, own money and stolen money per victim.

    mode="proportional"  an outflow carries each victim's money in the same ratio
                         as the sender's current balance (haircut).
    mode="fifo"          an outflow consumes the oldest money first.
    mode="full_freeze"   today's practice, for comparison: every account that ever
                         received stolen money is frozen in full. The split of its
                         balance into own/stolen uses proportional tracing, so the
                         stolen money it locks is the same as in "proportional".
    """

    def __init__(self, mode: str = "proportional"):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode
        self._own: dict[str, int] = {}
        self._stolen: dict[str, dict[str, int]] = {}
        self._lots: dict[str, deque[list]] = {}  # fifo only: [source, amount]
        self.losses: dict[str, int] = {}
        self.cashed_out: dict[str, int] = {}
        self.received_stolen: set[str] = set()
        self._last: tuple[int, int] | None = None

    # ---- replay -------------------------------------------------------------

    def run(self, events) -> "Tracer":
        for e in sorted(events, key=lambda e: (e.ts, e.eid)):
            self.apply(e)
        return self

    def apply(self, e: Event) -> None:
        key = (e.ts, e.eid)
        if self._last is not None and key <= self._last:
            raise ValueError(f"event {key} is out of order (last applied {self._last})")
        self._last = key

        if e.kind == "FRAUD":
            self.losses[e.victim] = self.losses.get(e.victim, 0) + e.amount
            self._put(e.dst, [(e.victim, e.amount)])
            self.received_stolen.add(e.dst)
        elif e.kind == "INCOME":
            self._put(e.dst, [(OWN, e.amount)])
        elif e.kind == "TRANSFER":
            pieces = self._take(e.src, e.amount)
            self._put(e.dst, pieces)
            if any(src is not OWN and amt > 0 for src, amt in pieces):
                self.received_stolen.add(e.dst)
        elif e.kind == "CASHOUT":
            for src, amt in self._take(e.src, e.amount):
                if src is not OWN:
                    self.cashed_out[src] = self.cashed_out.get(src, 0) + amt

    def _fifo(self) -> bool:
        return self.mode == "fifo"

    def _put(self, account: str, pieces: list[tuple[str | None, int]]) -> None:
        if self._fifo():
            lots = self._lots.setdefault(account, deque())
            for src, amt in pieces:
                if amt <= 0:
                    continue
                if lots and lots[-1][0] == src:
                    lots[-1][1] += amt
                else:
                    lots.append([src, amt])
            return
        self._own.setdefault(account, 0)
        stolen = self._stolen.setdefault(account, {})
        for src, amt in pieces:
            if src is OWN:
                self._own[account] += amt
            else:
                stolen[src] = stolen.get(src, 0) + amt

    def _take(self, account: str, amount: int) -> list[tuple[str | None, int]]:
        bal = self.balance(account)
        if amount > bal:
            raise ValueError(f"{account} sends {amount} but holds only {bal}")

        if self._fifo():
            lots = self._lots[account]
            pieces, need = [], amount
            while need:
                src, amt = lots[0]
                used = min(amt, need)
                pieces.append((src, used))
                need -= used
                if used == amt:
                    lots.popleft()
                else:
                    lots[0][1] -= used
            return pieces

        # Proportional: tie-break order is victims by sorted id, then own money.
        stolen = self._stolen[account]
        victims = sorted(v for v, amt in stolen.items() if amt > 0)
        weights = [stolen[v] for v in victims] + [self._own[account]]
        parts = split_largest_remainder(amount, weights)
        pieces = []
        for v, part in zip(victims, parts):
            stolen[v] -= part
            if stolen[v] == 0:
                del stolen[v]
            pieces.append((v, part))
        self._own[account] -= parts[-1]
        pieces.append((OWN, parts[-1]))
        return pieces

    # ---- queries ------------------------------------------------------------

    def accounts(self) -> list[str]:
        return sorted(self._lots) if self._fifo() else sorted(self._own)

    def own(self, account: str) -> int:
        if self._fifo():
            return sum(a for s, a in self._lots.get(account, ()) if s is OWN)
        return self._own.get(account, 0)

    def stolen(self, account: str) -> dict[str, int]:
        """Traced stolen money in `account`, per victim, sorted by victim id."""
        if self._fifo():
            out: dict[str, int] = {}
            for s, a in self._lots.get(account, ()):
                if s is not OWN:
                    out[s] = out.get(s, 0) + a
        else:
            out = self._stolen.get(account, {})
        return {v: out[v] for v in sorted(out) if out[v] > 0}

    def balance(self, account: str) -> int:
        return self.own(account) + sum(self.stolen(account).values())

    def lien(self, account: str) -> int:
        """Amount to freeze in `account` under this tracer's mode."""
        if self.mode == "full_freeze":
            return self.balance(account) if account in self.received_stolen else 0
        return sum(self.stolen(account).values())

    def legit_frozen(self, account: str) -> int:
        """Money frozen in `account` beyond its traced stolen money."""
        return self.lien(account) - sum(self.stolen(account).values())

    def recoverable(self, victim: str) -> int:
        """Victim's money still sitting in tracked accounts (not cashed out)."""
        return sum(self.stolen(a).get(victim, 0) for a in self.accounts())

    def allocate(self, account: str, recovered: int) -> dict[str, int]:
        return allocate(self.stolen(account), recovered)

    def check_invariants(self) -> None:
        """Raise AssertionError if any §8.7 invariant is broken."""
        for a in self.accounts():
            assert self.own(a) >= 0, f"negative own money in {a}"
            for v, amt in self.stolen(a).items():
                assert amt >= 0, f"negative stolen money for {v} in {a}"
        for v in sorted(self.losses):
            remaining = self.recoverable(v)
            gone = self.cashed_out.get(v, 0)
            assert 0 <= remaining <= self.losses[v], f"traced total out of range for {v}"
            assert remaining + gone == self.losses[v], f"conservation broken for {v}"
