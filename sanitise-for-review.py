"""Strip remediation history from a copy of the optimizer source.

Run from the root of the review copy:  python sanitise.py

Removes comments whose purpose is to say what was wrong before, and any
reference to a finding ID.  Leaves every comment that describes what the
code does, and leaves the code itself untouched.
"""
import pathlib
import re

PKG = pathlib.Path("scripts/cash_optimizer_poc")

# Phrases that name a finding.
PHRASES = [r"\s*\(finding F\d+\)", r"\s*\(F\d+\)", r"\s*\(audit F\d+\)",
           r"\s*, audit F\d+", r"\s*\bF\d+[,:]?\s*(?=[A-Z])"]

# Comment blocks that exist to explain a past defect.  Each entry is
# (file, first line of the block, first line to keep after it).
BLOCKS = [
    ("models.py",
     "    # CBC 2.10.3 (the solver PuLP bundles) returns provably suboptimal",
     "    # ``None`` picks the best solver"),
    ("models.py",
     "    # Computed on access rather than stored.",
     "    @property\n    def big_m"),
    ("models.py",
     "    This is the single definition.",
     '    """\n\n    credit_carry_cost'),
    ("models.py",
     "    # It is NOT \"sterling is 365 and everything else is 360\"",
     "    day_count_basis"),
    ("optimizer.py",
     "    #: Preference order when Config.solver_name is None.",
     "    SOLVER_PREFERENCE"),
    ("optimizer.py",
     "    #: A plan must beat the incumbent",
     "    IMPROVEMENT_TOLERANCE"),
    ("optimizer.py",
     "    Raised for any terminal status",
     "    Subclasses ``RuntimeError``"),
    ("cash_manager.py",
     "            # each was dealt at.  This used to convert the netted foreign",
     "            base_trade_impact"),
    ("cash_manager.py",
     "        # Take a copy when overriding, rather than writing through to the",
     "        if constraints is not None:"),
]

REPLACEMENTS = [
    ("optimizer.py",
     "    SOLVER_PREFERENCE",
     "    #: Solver preference order; the first available one is used.\n"
     "    SOLVER_PREFERENCE"),
    ("cash_manager.py",
     "            # Base impact from foreign trades settling today, at the rate\n",
     "            # Base impact from foreign trades settling today, at the rate\n"
     "            # each was dealt at.\n"),
]


def main() -> None:
    for path in sorted(PKG.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        original = text

        for name, start, end in BLOCKS:
            if path.name != name:
                continue
            i = text.find(start)
            if i == -1:
                continue
            j = text.find(end, i)
            if j == -1:
                continue
            text = text[:i] + text[j:]

        for name, old, new in REPLACEMENTS:
            if path.name == name and old in text:
                text = text.replace(old, new, 1)

        for pattern in PHRASES:
            text = re.sub(pattern, "", text)

        # tidy any comment left with nothing after its marker
        text = re.sub(r"\n\s*#\s*\n(\s*#)", r"\n\1", text)

        if text != original:
            path.write_text(text, encoding="utf-8")
            print(f"  cleaned {path.name}")

    print("\nremaining references (should be none):")
    hits = 0
    suspect = re.compile(
        r"\bF\d{1,2}\b|provably suboptimal|CBC 2\.10\.3|used to be declared"
        r"|used to convert|Previously these returned|used to be filled in"
        r"|silently changed|the audit", re.I)
    for path in sorted(PKG.glob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if suspect.search(line):
                print(f"  {path.name}:{n}: {line.strip()[:90]}")
                hits += 1
    if not hits:
        print("  none")


if __name__ == "__main__":
    main()
