"""Pre-flight checks that must pass before this repository is made public.

    python scripts/preflight.py            # report and exit non-zero on any failure
    python scripts/preflight.py --verbose  # also list every check that passed

Work item 4.6. Deliberately mechanical: it checks the things a human forgets under deadline,
and it checks git HISTORY as well as the working tree, because the two have already disagreed
once on this project. A real phone number was redacted from the tree on 3 September and
survived in history and in GitHub's pull-request refs, which is why the repository had to be
deleted and recreated. See amendment A6.

Two categories of check, and the difference matters:

  SECRETS AND PERSONAL DATA - a failure here is not recoverable by editing the tree once the
  repo is public. Cloned objects and pull-request refs persist. These are the ones worth
  being paranoid about.

  CLAIMS - things this project has committed to never asserting: DPDP compliance before the
  obligations commence, a numeric call cap attributed to the RBI, and the word "accuracy" as
  a headline metric. A failure here is embarrassing rather than dangerous, and fixable.

Exit code is 0 only if every check passes. Nothing here modifies the repository.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]

#: Files whose whole purpose is to discuss what must not appear. A hit inside one of these is
#: the project documenting its own rule, not breaking it. Kept short and named individually -
#: a broad glob here would hollow out every check below.
CLAIM_EXEMPT = {
    "scripts/preflight.py",
    "docs/metric-definition.md",
    "docs/pr-review-archive.md",
    "README.md",                 # states the negation: 'Not "DPDP-compliant"'
    "CLAUDE.md",
}

#: Numbers that are obviously synthetic. Anything else matching an Indian mobile shape is
#: treated as potentially real, because that is the error that cannot be walked back.
KNOWN_SYNTHETIC = {"919812345670", "919123456780", "919999999999", "919000090000",
                   "911234567890", "919876543210"}


#: A line that STATES one of these rules necessarily contains the forbidden phrase. The whole
#: repository is built on writing its own constraints down, so a checker that cannot tell
#: "never say X" from "X" would flag every place the rule is recorded and nothing else.
#: Matched per-line, so the exemption is as narrow as possible.
_RULE_MARKER = re.compile(
    r"\bnowhere\b|\bnever\b|\bnot\s+in\b|\bmust\s+not\b|\bshould\s+not\b|\bcannot\b|"
    r"\bforbidden\b|\bbanned\b|\bavoid\b|overclaim|\bassert\b|\bno\s+[\"'`]|^\s*-\s*no\b",
    re.I | re.M)


def _states_the_rule(line: str) -> bool:
    return bool(_RULE_MARKER.search(line))


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""
    hits: list[str] = field(default_factory=list)
    fatal: bool = True          # False = advisory, does not fail the run


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=REPO_ROOT).stdout


def _tracked_files() -> list[str]:
    return [f for f in _git("ls-files").splitlines() if f.strip()]


def _read(rel: str) -> str:
    p = REPO_ROOT / rel
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return ""


def _search_tree(pattern: re.Pattern, skip: set[str] = frozenset()) -> list[str]:
    hits = []
    for rel in _tracked_files():
        if rel in skip:
            continue
        for m in pattern.finditer(_read(rel)):
            hits.append(f"{rel}: {m.group(0)[:80]}")
    return hits


def _publishable_refs() -> list[str]:
    """The refs a reader of the public repository could reach.

    Local `backup/*` branches are excluded deliberately and by name. They exist as recovery
    points for the history rewrites and are never pushed; counting them reports the
    pre-scrub zrok hostname as a live exposure, which is exactly the false alarm that teaches
    people to ignore a checker.
    """
    refs = [r for r in _git("for-each-ref", "--format=%(refname)",
                            "refs/heads", "refs/remotes").splitlines() if r.strip()]
    return [r for r in refs if not r.startswith("refs/heads/backup/")]


def _search_history_blobs(pattern: re.Pattern) -> list[str]:
    """Search the CONTENT of every object reachable from a publishable ref.

    `git log -S` was the first approach and it is the wrong tool: it reports commits whose
    occurrence-count CHANGED, so a string that was added and then correctly scrubbed still
    shows up, and so does the scrubbing commit itself. That produced six hits here, every one
    a commit whose subject line was literally about scrubbing it from history. What matters
    is whether the string is still REACHABLE, so this reads the blobs.
    """
    refs = _publishable_refs()
    if not refs:
        return []
    listing = _git("rev-list", "--objects", *refs)
    shas = [line.split()[0] for line in listing.splitlines() if line.strip()]
    if not shas:
        return []
    proc = subprocess.run(["git", "cat-file", "--batch"], input="\n".join(shas),
                          capture_output=True, text=True, errors="replace", cwd=REPO_ROOT)
    return sorted({m.group(0) for m in pattern.finditer(proc.stdout)})


# ---------------------------------------------------------------------------------------
# secrets and personal data
# ---------------------------------------------------------------------------------------

def check_env_not_tracked() -> Result:
    """The first version searched history for the string RAZORPAY_KEY_SECRET and reported
    four hits, every one of them the variable's NAME in a doc or a script rather than a
    value. A secret scanner that fires on the word "secret" is worse than no scanner, because
    people learn to skip its output. This looks for a committed .env FILE instead; actual key
    values are covered by the next check."""
    tracked = [f for f in _tracked_files() if f == ".env" or f.startswith(".env.")]
    ignored = bool(re.search(r"^\.env", _read(".gitignore"), re.M))
    refs = _publishable_refs() or ["HEAD"]
    ever = sorted({line.split(maxsplit=1)[1] for line in
                   _git("rev-list", "--objects", *refs).splitlines()
                   if len(line.split(maxsplit=1)) == 2
                   and line.split(maxsplit=1)[1].startswith(".env")})
    ok = not tracked and ignored and not ever
    return Result(".env is untracked, gitignored, and never committed", ok,
                  f"tracked={tracked or 'none'} gitignored={ignored}", tracked + ever)


def check_no_api_keys() -> Result:
    pat = re.compile(r"rzp_(test|live)_[A-Za-z0-9]{8,}|whsec_[A-Za-z0-9]{8,}|sk-ant-[A-Za-z0-9-]{8,}")
    hits = _search_tree(pat, skip={"scripts/preflight.py"})
    hits += [f"still reachable in history: {h}" for h in _search_history_blobs(pat)]
    return Result("no Razorpay or Anthropic key in tree or history", not hits, "", hits)


def check_no_tunnel_hostname() -> Result:
    pat = re.compile(r"[a-z0-9-]+\.shares\.zrok\.io|rzp-wh-[0-9a-f]{6,}")
    hits = _search_tree(pat, skip={"scripts/preflight.py"})
    hits += [f"still reachable in history: {h}" for h in _search_history_blobs(pat)]
    return Result("no live tunnel hostname in tree or history", not hits, "", hits)


def check_no_personal_phone_numbers() -> Result:
    """Any Indian mobile shape that is not a known synthetic placeholder.

    Deliberately over-broad. A false positive costs a glance; a false negative is the failure
    that cannot be undone once the repository is public.
    """
    # The boundaries are load-bearing and had to be widened twice. Digit-only boundaries
    # still matched ten-digit runs inside SHA-256 hashes, where a letter sits either side:
    # `...a16ed8e7552389303be4d...` contains a perfectly good "phone number". Excluding any
    # adjacent alphanumeric fixes it, because a real number is delimited by quotes, spaces
    # or a plus sign - never by a hex digit.
    pat = re.compile(r"(?<![0-9A-Za-z])(?:\+?91)?([6-9]\d{9})(?![0-9A-Za-z])")
    hits = []
    for rel in _tracked_files():
        if rel == "scripts/preflight.py":
            continue
        for m in pat.finditer(_read(rel)):
            digits = "91" + m.group(1)
            if digits not in KNOWN_SYNTHETIC:
                hits.append(f"{rel}: {m.group(0)}")
    return Result("no non-synthetic phone number in the tree", not hits,
                  f"{len(KNOWN_SYNTHETIC)} placeholders allow-listed", hits)


def check_no_personal_email() -> Result:
    pat = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    allowed = re.compile(r"@(example\.(com|org|net)|razorpay\.com|iitp\.ac\.in|users\.noreply\.github\.com)$",
                         re.I)
    hits = []
    for rel in _tracked_files():
        if rel == "scripts/preflight.py":
            continue
        for m in pat.finditer(_read(rel)):
            if not allowed.search(m.group(0)):
                hits.append(f"{rel}: {m.group(0)}")
    return Result("no third-party email address in the tree", not hits, "", hits)


# ---------------------------------------------------------------------------------------
# claims this project has committed to never making
# ---------------------------------------------------------------------------------------

def check_no_dpdp_compliance_claim() -> Result:
    """The substantive DPDP obligations commence 14 May 2027. "Designed for" is the true
    claim. The phrase is permitted only inside an explicit negation."""
    hits = []
    for rel in _tracked_files():
        if rel in CLAIM_EXEMPT:
            continue
        for n, line in enumerate(_read(rel).splitlines(), 1):
            if re.search(r"DPDP[- ]compliant", line, re.I) and not _states_the_rule(line):
                hits.append(f"{rel}:{n}: {line.strip()[:80]}")
    return Result("no unqualified DPDP-compliance claim", not hits, "", hits)


def check_no_numeric_call_cap_attributed_to_rbi() -> Result:
    """RBI says only "excessively calling" and names no figure. The 7-in-7 rule is US
    Regulation F and has no force in India; it is adopted here as a labelled policy choice."""
    # A number near "RBI" is fine when the text says whose number it is. Both places this
    # fired on the first run were doing exactly that - naming US Regulation F as the source
    # and the RBI standard as qualitative - which is the distinction the rule exists to
    # protect, not a breach of it.
    attributed = re.compile(r"Reg(ulation)?\s*F|United States|\bUS\b|policy choice|"
                            r"no force in India|not\s+RBI|qualitative", re.I)
    # Per-LINE, not a character window. A 200-character window with re.S spilled from one
    # markdown table row into the next, so a row about the 08:00-19:00 contact window was
    # flagged for a "7 contacts / 7 days" figure that lives in the row below it - a row which
    # says in terms that the number is US Reg F and has no force in India. An attribution
    # that sits one line away is still an attribution.
    hits = []
    for rel in _tracked_files():
        if rel in CLAIM_EXEMPT:
            continue
        for n, line in enumerate(_read(rel).splitlines(), 1):
            if "RBI" not in line:
                continue
            if re.search(r"\b\d+\s*(calls?|contacts?|attempts?)\b", line, re.I) \
                    and not attributed.search(line) and not _states_the_rule(line):
                hits.append(f"{rel}:{n}: {' '.join(line.split())[:90]}")
    return Result("no numeric call cap attributed to the RBI", not hits, "", hits)


def check_accuracy_is_absent() -> Result:
    """`accuracy` is the wrong metric for a cost-sensitive recovery decision, and the project
    committed to it appearing nowhere."""
    hits = []
    for rel in _tracked_files():
        if rel in CLAIM_EXEMPT or not rel.endswith((".py", ".md", ".json")):
            continue
        for n, line in enumerate(_read(rel).splitlines(), 1):
            if re.search(r"\baccuracy\b", line, re.I) and not _states_the_rule(line):
                hits.append(f"{rel}:{n}: {line.strip()[:80]}")
    return Result("the word 'accuracy' appears nowhere", not hits, "", hits)


# ---------------------------------------------------------------------------------------
# repository hygiene
# ---------------------------------------------------------------------------------------

def check_licence_present() -> Result:
    files = [f for f in _tracked_files() if f.upper().startswith(("LICENSE", "LICENCE"))]
    return Result("a LICENSE file is present", bool(files),
                  "a public repo without one reserves all rights by default", [], fatal=True)


def check_figures_match_the_artifact() -> Result:
    r = subprocess.run([sys.executable, "scripts/render_docs.py", "--check"],
                       capture_output=True, text=True, cwd=REPO_ROOT)
    return Result("every documented figure matches results/metrics.json", r.returncode == 0,
                  "", [] if r.returncode == 0 else [r.stdout.strip(), r.stderr.strip()])


CHECKS = [
    check_env_not_tracked,
    check_no_api_keys,
    check_no_tunnel_hostname,
    check_no_personal_phone_numbers,
    check_no_personal_email,
    check_no_dpdp_compliance_claim,
    check_no_numeric_call_cap_attributed_to_rbi,
    check_accuracy_is_absent,
    check_licence_present,
    check_figures_match_the_artifact,
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true", help="show passing checks too")
    args = ap.parse_args()

    print("\nPRE-FLIGHT - run before making this repository public\n" + "=" * 74)
    failed: list[Result] = []
    for fn in CHECKS:
        r = fn()
        if r.ok:
            if args.verbose:
                print(f"  PASS  {r.name}" + (f"   [{r.detail}]" if r.detail else ""))
        else:
            failed.append(r)
            print(f"  FAIL  {r.name}")
            if r.detail:
                print(f"        {r.detail}")
            for h in r.hits[:8]:
                print(f"        - {h}")
            if len(r.hits) > 8:
                print(f"        ... and {len(r.hits) - 8} more")

    print("=" * 74)
    if failed:
        print(f"{len(failed)} of {len(CHECKS)} checks FAILED. Do not make the repository "
              f"public yet.\n")
        print("Note: for a secrets or personal-data failure, editing the working tree is not")
        print("enough. Objects survive in history and in GitHub's pull-request refs, which is")
        print("why this repository was recreated once already. See amendment A6.\n")
        return 1
    print(f"All {len(CHECKS)} checks passed.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
