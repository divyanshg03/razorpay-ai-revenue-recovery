"""Phase 4 exit criteria — the packaging must not be able to lie.

Phase 3 established the number. Phase 4 is where a submission usually starts overstating it,
because prose is easier to edit than an artifact. So the tests here are not about whether the
README reads well. They are about whether it *can* disagree with `results/metrics.json`, and
whether the disclosure can drift below the headline while nobody is looking.

Three properties, each of which failed at least once in a real project before it was pinned:

  - every rupee figure in the README comes from a generated block
  - every limitation the artifact carries appears in the README, counted, none dropped
  - the simulated-cohort disclosure appears BEFORE the headline figure, in file order
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
README = REPO / "README.md"
METRICS = REPO / "results" / "metrics.json"

GEN = re.compile(r"<!-- generated:([a-z0-9-]+) -->(.*?)<!-- /generated:\1 -->", re.S)


@pytest.fixture(scope="module")
def m():
    return json.loads(METRICS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def readme():
    return README.read_text(encoding="utf-8")


def _outside_generated(text: str) -> str:
    """The README with every generated block removed - i.e. the hand-written part."""
    return GEN.sub("", text)


# ---------------------------------------------------------------------------------------
# 4.1 every figure is generated
# ---------------------------------------------------------------------------------------

def test_readme_has_the_generated_blocks_it_claims():
    names = {m_.group(1) for m_ in GEN.finditer(README.read_text(encoding="utf-8"))}
    assert {"readme-headline", "readme-npci", "readme-failures", "readme-limitations",
            "readme-reproduce"} <= names, names


def test_no_rupee_figure_is_hand_typed_in_the_README(readme):
    """A rupee amount outside a generated block is a figure nobody can check.

    This is the rule the repo has had since Phase 0 - "if it isn't in the artifact, it
    doesn't go in the prose" - and Phase 3 proved it needed enforcing rather than stating:
    docs/phase-3.md sat for a day claiming a headline the artifact had already superseded.
    """
    hand_written = _outside_generated(readme)
    # Any "Rs" followed by digits. Currency words without a number are fine.
    offenders = re.findall(r"Rs\s?[\d,]+(?:\.\d+)?", hand_written)
    assert not offenders, f"hand-typed rupee figures outside generated blocks: {offenders}"


def test_render_check_covers_the_README_and_is_clean():
    """`--check` must actually include README.md, not just the phase doc."""
    from importlib import util
    spec = util.spec_from_file_location("rd", REPO / "scripts" / "render_docs.py")
    rd = util.module_from_spec(spec)
    spec.loader.exec_module(rd)
    assert "README.md" in rd.TARGETS, rd.TARGETS

    r = subprocess.run([sys.executable, "scripts/render_docs.py", "--check"],
                       capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"


def test_headline_in_the_README_equals_the_artifact(readme, m):
    """Spot-check the actual number, not just that a renderer ran."""
    block = next(g.group(2) for g in GEN.finditer(readme) if g.group(1) == "readme-headline")
    expected = f"Rs {m['headline']['net_incremental_rupees']:,.0f}"
    assert expected in block, (expected, block[:400])


# ---------------------------------------------------------------------------------------
# 4.2 limitations are complete, and placed before the claim
# ---------------------------------------------------------------------------------------

def test_every_limitation_in_the_artifact_reaches_the_README(readme, m):
    """Adding a limitation to batch.py and forgetting the README must fail the build.

    The count is asserted as well as the content, because a renderer that silently truncates
    is exactly the failure being guarded against, and it would slip past a check that only
    looked for the text of the limitations it did emit.
    """
    block = next(g.group(2) for g in GEN.finditer(readme) if g.group(1) == "readme-limitations")
    numbered = re.findall(r"^\s*(\d+)\.\s", block, re.M)
    assert len(numbered) == len(m["limitations"]), (
        f"README lists {len(numbered)} limitations, artifact has {len(m['limitations'])}")

    flat = " ".join(block.split())
    for lim in m["limitations"]:
        head = " ".join(lim.split())[:60]
        assert head in flat, f"limitation missing from README: {head!r}"


def test_the_two_phase3_self_criticisms_are_present(m):
    """The findings Phase 3 made about ITSELF, which a panel would otherwise find first."""
    joined = " ".join(m["limitations"]).lower()
    assert "needs_customer_action" in joined and "declined" in joined, \
        "the deliberately-unfixed weak spot (A2) is not disclosed"
    assert "pre-registered prediction" in joined and "did not hold" in joined, \
        "the failed pre-registration (A1) is not disclosed"


def test_the_simulator_disclosure_comes_before_the_headline_number(readme):
    """Ordering is the whole ethic of this README, so it is asserted rather than trusted.

    The temptation when packaging is to lead with the money and let the disclosure drift down
    the page. That single edit turns an honest project into a dishonest one, and it is
    invisible to every other test in this suite - the figures would still all be generated
    and correct.
    """
    disclosure = readme.lower().find("cohort is simulated")
    headline = readme.find("<!-- generated:readme-headline -->")
    assert disclosure != -1, "the simulated-cohort disclosure is missing entirely"
    assert headline != -1
    assert disclosure < headline, \
        "the headline figure appears above the simulated-cohort disclosure"


def test_the_failure_list_comes_before_the_architecture_section(readme):
    """What it failed to recover is part of the result, not an appendix to the design."""
    failures = readme.find("<!-- generated:readme-failures -->")
    architecture = readme.find("## How it is built")
    assert -1 not in (failures, architecture)
    assert failures < architecture


# ---------------------------------------------------------------------------------------
# 4.3 / claims the README is not allowed to make
# ---------------------------------------------------------------------------------------

def test_readme_never_claims_dpdp_compliance(readme):
    """Substantive DPDP obligations commence 14 May 2027. "Designed for" is the true claim.

    The phrase may appear only inside an explicit negation, which is why this checks the
    surrounding words rather than banning the string.
    """
    for match in re.finditer(r'.{0,40}DPDP.compliant', readme, re.I):
        context = match.group(0).lower()
        assert "not" in context or "never" in context, f"unqualified claim: {match.group(0)!r}"
    assert "14 May 2027" in readme, "the correct commencement date is not stated"


def test_readme_attributes_no_numeric_call_cap_to_rbi(readme):
    """RBI says only "excessively calling" and names no figure. The 7-in-7 rule is US Reg F."""
    for match in re.finditer(r'RBI.{0,160}', readme, re.S):
        seg = match.group(0)
        assert not re.search(r"\b\d+\s*(calls|contacts|attempts)\b", seg, re.I), seg


def test_the_word_accuracy_appears_nowhere_in_the_README(readme):
    assert "accuracy" not in readme.lower()


def test_readme_states_the_comparison_is_against_the_incumbent(readme):
    """Beating do-nothing proves nothing; the README must say what it beat."""
    low = readme.lower()
    assert "do-nothing" in low or "do nothing" in low
    assert "ladder" in low


# ---------------------------------------------------------------------------------------
# 4.4 the architecture claim the README makes must actually hold
# ---------------------------------------------------------------------------------------

#: The ONLY names `engine/` may take from `llm/`. Both are vocabulary for describing a reply
#: that has already been parsed - an enum and a frozen dataclass. Neither can invoke anything.
PERMITTED_LLM_IMPORTS = {"Intent", "ParsedReply"}


def test_the_engine_cannot_reach_a_language_model():
    """The claim a panel will probe first, pinned as an enumerated allow-list.

    The README originally said "engine/machine.py has no import path to llm/". That was
    FALSE - it imports Intent and ParsedReply - and the wording was corrected rather than the
    code, because the import is correct and the sentence was lazy. What actually matters is
    narrower and stronger: the engine may name the reply vocabulary, but it must not import
    anything capable of *calling* a model, and nothing under engine/ may open a socket.

    An allow-list rather than a deny-list, so a newly added model-invoking helper fails here
    by default instead of needing to be predicted.
    """
    import ast

    engine_dir = REPO / "src" / "recovery" / "engine"
    offenders: list[str] = []
    for path in sorted(engine_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "llm" in node.module:
                for alias in node.names:
                    if alias.name not in PERMITTED_LLM_IMPORTS:
                        offenders.append(f"{path.name} imports {alias.name} from {node.module}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "llm" in alias.name:
                        offenders.append(f"{path.name} imports module {alias.name}")
    assert not offenders, offenders


def test_no_engine_module_performs_network_io():
    """A decision path that can open a socket is a decision path that can call a model."""
    import ast

    banned = {"urllib", "http", "requests", "socket", "httpx", "ollama"}
    offenders: list[str] = []
    for path in sorted((REPO / "src" / "recovery" / "engine").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                if n.split(".")[0] in banned:
                    offenders.append(f"{path.name}: {n}")
    assert not offenders, offenders


@pytest.fixture(scope="module")
def demo_out():
    """One offline run, shared by every assertion below. Headless, no model, no colour."""
    r = subprocess.run([sys.executable, "scripts/demo.py"],
                       capture_output=True, text=True, cwd=REPO, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout


def test_the_demo_puts_a_real_razorpay_event_through_the_shipped_receiver(demo_out):
    """The demo's first answer to "does any of this touch Razorpay?" must be evidence.

    The event is the one phase 0 captured from Razorpay's servers through a zrok tunnel, and
    it goes through `WebhookIngest` - verified, deduplicated, and rejected when a byte of the
    body is edited. If the fixture or the receiver stops agreeing, this fails rather than the
    scene quietly degrading into a printed JSON blob.
    """
    log = (REPO / "results" / "phase0" / "0.4c-received-events.jsonl").read_text(encoding="utf-8")
    real = [json.loads(x) for x in log.splitlines() if x.strip()]
    razorpay_sent = [r for r in real if not r["event_id"].startswith("evt_")]
    assert razorpay_sent, "the phase-0 log no longer contains a Razorpay-originated event"
    event = razorpay_sent[0]

    assert event["event_id"] in demo_out
    assert "200 queued" in demo_out, "the real event was not accepted by the receiver"
    assert "duplicate ignored" in demo_out, "at-least-once delivery is not demonstrated"
    assert "400 invalid signature" in demo_out, "a tampered body was not rejected on screen"
    assert "only real payment in this demo" in demo_out, \
        "the demo must not let one real event lend credibility to the simulated cohort"


def test_the_demo_shows_the_replies_that_used_to_break_the_parser(demo_out):
    """The five an interviewer types first. Two of them are not in English."""
    for reply in ("My father passed away. Stop messaging me.",
                  "my mother died 2 days ago",
                  "yeh messages band karo"):
        assert reply in demo_out, f"the demo no longer shows: {reply}"
    # The statutory stop must be the outcome on screen, not a hardship pause.
    line = next(ln for ln in demo_out.splitlines()
                if "stopped: opt_out" in ln)
    assert "opt_out" in line


def test_the_demo_prints_the_artifacts_own_headline_not_a_remembered_one(demo_out, m):
    """The demo reads results/metrics.json at run time, so it cannot drift from the README.

    A demo that prints a number from memory is the same failure the README tests exist to
    prevent, one screen to the left of it.
    """
    headline = f"Rs {m['headline']['net_incremental_rupees']:,.0f}"
    assert headline in demo_out, headline
    control = m["primary_cohort_21d"]["spread_retry_control"][
        "decisioning_is_worth__C_vs_D_diagnosed"]
    if not control["excludes_zero"]:
        assert "CROSSES ZERO" in demo_out, \
            "the control's interval crosses zero and the demo does not say so"


@pytest.fixture(scope="module")
def capped():
    return json.loads((REPO / "results" / "npci-cap-rerun.json").read_text(encoding="utf-8"))


def test_the_capped_rerun_stays_inside_the_rule_it_names(capped):
    """The artifact that answers "NPCI allows four attempts" must itself take four.

    A companion artifact measured under rules it describes loosely is worse than no companion
    artifact, because it invites the same question twice.
    """
    days = capped["schedule"]["engine_and_controls"]
    assert len(days) == 3, f"OC-215-A allows three retries after the attempt, got {days}"
    assert 0 not in days, "day 0 is the charge that failed, not a retry"
    assert capped["schedule"]["incumbent"] == [1, 2, 3], "Razorpay's ladder excludes T+0"
    assert capped["policy_overrides"]["max_retries_per_debt"] == 3
    assert capped["window_anchored_on"] == "each debt's own failure date"
    assert not capped["working_tree_dirty"], \
        "regenerate it from a committed tree: head_commit cannot reproduce a dirty one"
    assert capped["arms"]["C_engine"]["n"] > 0 and capped["arms"]["B_incumbent"]["n"] > 0


def test_the_capped_figures_in_the_README_equal_their_artifact(readme, capped):
    """The capped run is reported in the README, so it is held to the headline's rule: read,
    never typed, and checked against the file it came from rather than against a renderer
    that merely ran."""
    block = next(g.group(2) for g in GEN.finditer(readme) if g.group(1) == "readme-npci")
    net = capped["comparisons"]["headline__C_vs_B"]["net_incremental_total_rupees"]
    assert f"Rs {net:,.0f}" in block, net
    assert f"{capped['arms']['C_engine']['recovery_rate'] * 100:.2f}%" in block
    decisioning = capped["comparisons"]["decisioning_is_worth__C_vs_D_diagnosed"]
    word = "excludes" if decisioning["excludes_zero"] else "crosses"
    assert f"which {word} zero" in " ".join(block.split()), \
        "the README states a significance the artifact does not support"


def test_the_readme_never_presents_the_capped_run_as_the_headline(readme):
    """Beside the headline, never instead of it: the frozen definition fixes the headline's
    configuration, and swapping in a re-run under new rules is what the freeze prevents."""
    headline = readme.find("<!-- generated:readme-headline -->")
    capped = readme.find("<!-- generated:readme-npci -->")
    assert -1 not in (headline, capped) and headline < capped


def test_the_demo_reports_the_capped_rerun_from_the_artifact(demo_out, capped):
    """Scene 14's numbers are read, not remembered - the same rule as the headline."""
    assert "OC-215-A" in demo_out
    assert f"{capped['arms']['C_engine']['recovery_rate']:.2%}" in demo_out
    headline = capped["comparisons"]["headline__C_vs_B"]["net_incremental_total_rupees"]
    assert f"Rs {headline:,.0f}" in demo_out


def test_the_demo_ledger_is_dated_by_simulated_day_not_by_wall_clock(demo_out):
    """Scene 12 replays a fortnight of decisions, so it must show a fortnight of dates.

    Every record used to be stamped 3 September at 10:00 plus one second per record, from a
    virtual clock that started at day 0 - so the replay showed the customer's reply BEFORE
    the message that drew it, on a screen whose whole subject is an ordered trail.
    """
    dates = set(re.findall(r"^\s+(2026-09-\d\d) \d\d:\d\d:\d\d  \w+", demo_out, re.M))
    assert len(dates) >= 3, f"the replay is collapsed onto {dates or 'no'} date(s)"


def test_the_demo_runs_clean_offline_and_shows_the_gate_firing():
    """The demo is the artifact the track is actually judged on, so it must not rot.

    Run headless, with no model: that path has to work, because a judge reproducing this on
    a machine without Ollama is the likeliest way it gets watched. The batch measurement is
    template-composed for the same reason, so running offline is honest rather than degraded.

    The MISMATCH assertion is the valuable one. Each copy-gate probe declares the rule it is
    meant to trip and the demo prints whether the gate agreed. The first version of that
    scene showed five clean rejections, one of which had actually been rejected for "payment
    link missing" - the shaming rule was never exercised and the screen implied it had been.
    If a gate pattern drifts so a probe passes for the wrong reason, this fails.
    """
    r = subprocess.run([sys.executable, "scripts/demo.py"],
                       capture_output=True, text=True, cwd=REPO, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    out = r.stdout

    assert "MISMATCH" not in out, "a copy-gate probe tripped a different rule than it claims"

    for rule in ("discount_or_offer", "false_urgency", "scarcity", "threat_or_shaming",
                 "fabricated_amount"):
        assert rule in out, f"the gate never demonstrated {rule}"

    # The loop's load-bearing moments, each of which a panel will ask about.
    for beat in ("DIAGNOSIS", "GUARDRAILS", "COPY GATE", "AUDIT TRAIL",
                 "stop_reason=opt_out", "stop_reason=payment_received",
                 "hash chain intact          True"):
        assert beat in out, f"missing from the demo: {beat}"

    assert "cohort is simulated" in out, "the demo must disclose the simulator before it ends"


def test_the_demo_uses_the_real_components_not_a_reimplementation():
    """A demo that re-implements the system is a demo of the demo.

    The first version of this asserted on import STRINGS, which a dead import satisfies - and
    two dead imports were indeed sitting there when a reviewer looked. It now parses the file
    and requires every imported name to be referenced, so an import can no longer stand in as
    evidence that a component is actually being exercised.
    """
    import ast

    path = REPO / "scripts" / "demo.py"
    src = path.read_text(encoding="utf-8")
    for module in ("recovery.engine.machine", "recovery.engine.policy",
                   "recovery.llm.copy_gate", "recovery.llm.composer",
                   "recovery.llm.parser", "recovery.ledger.audit",
                   "recovery.evaluation.baselines"):
        assert module in src, f"demo does not import {module}"

    tree = ast.parse(src)
    used = ({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} |
            {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)})
    dead = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                if name != "annotations" and name not in used:
                    dead.append(name)
    assert not dead, f"imported but never used, so nothing proves it runs: {dead}"


def test_the_demo_reads_the_incumbent_schedule_from_the_code():
    """The comparison's other half must not be a typed string.

    `demo.py` hand-typed "days 0, 1, 2, 3" while reading its own schedule from
    `retry_schedule()`, which made the incumbent row the one number on screen not taken from
    the code - on the slide whose whole subject is the fairness of the comparison.
    """
    src = (REPO / "scripts" / "demo.py").read_text(encoding="utf-8")
    assert "INCUMBENT_RETRY_DAYS" in src
    assert '"days 0, 1, 2, 3' not in src, "the incumbent schedule is hand-typed again"


def test_the_demo_never_claims_the_same_retry_budget_as_the_incumbent():
    """The engine takes more attempts than the baseline, and must say so.

    `retry_schedule()` yields six; the incumbent takes four. Claiming "the same budget" while
    spread does the work is a fairness claim about the headline, and it was false.
    """
    from recovery.engine.policy import Policy, retry_schedule
    from recovery.evaluation.baselines import INCUMBENT_RETRY_DAYS

    assert len(retry_schedule(Policy())) > len(INCUMBENT_RETRY_DAYS), \
        "if these ever match, revisit the wording below rather than deleting this test"

    for path in (REPO / "scripts" / "demo.py", REPO / "src" / "recovery" / "engine" / "machine.py"):
        text = path.read_text(encoding="utf-8")
        for claim in ("the same budget", "the same number of attempts"):
            # Permitted only where the file is explaining that the claim was wrong.
            for line in text.splitlines():
                if claim in line:
                    assert "NOT the same" in line or "previously read" in line, \
                        f"{path.name}: unqualified '{claim}'"


def test_preflight_passes_on_the_current_tree():
    """Work item 4.6's gate. It must be green before the repository is made public."""
    r = subprocess.run([sys.executable, "scripts/preflight.py"],
                       capture_output=True, text=True, cwd=REPO, timeout=900)
    assert r.returncode == 0, r.stdout[-3000:]


#: Payloads are ASSEMBLED rather than written out, so this file does not itself contain a
#: key-shaped string, a live-looking hostname or a non-placeholder phone number. Spelling
#: them literally made `preflight` flag its own test file - correctly - and the temptation
#: then is to exempt the tests from scanning, which is how a secret scanner gets a blind spot
#: exactly where people paste examples.
@pytest.mark.parametrize("label,payload", [
    ("razorpay_key", "key = " + "rzp_" + "test_" + "ABCDEFGH12345678"),
    ("anthropic_key", "token = " + "sk-" + "ant-" + "api03-" + "X" * 12),
    ("zrok_hostname", "https://" + "rzp-wh-" + "deadbeef" + ".shares." + "zrok.io/webhook"),
    ("real_phone", "contact: +" + "91" + "9" + "000000001"),
    ("third_party_email", "write to " + "someone" + "@" + "gmail" + ".com"),
    ("dpdp_claim", "This system is DPDP-" + "compliant today."),
    ("rbi_numeric_cap", "RB" + "I limits you to 7 contacts per week."),
    ("accuracy_metric", "We report " + "accur" + "acy as the headline metric."),
])
def test_preflight_actually_catches_each_violation(label, payload):
    """A sweep that only ever passes is a rubber stamp.

    Every check is exercised against a planted violation, because the first run of this
    script produced seven findings and five of them were false positives - ten-digit windows
    inside SHA-256 hashes, a character window spilling across markdown table rows, and lines
    that state a rule being read as breaking it. Tuning that noise out is exactly where a
    checker quietly stops checking, so each rule now has to prove it still bites.

    The probe is force-added to the index because `preflight` reads tracked files, then
    removed again. Failing loudly is the desired outcome here.
    """
    probe = REPO / "docs" / f"_preflight_probe_{label}.md"
    rel = str(probe.relative_to(REPO)).replace("\\", "/")
    try:
        probe.write_text(f"# probe\n\n{payload}\n", encoding="utf-8")
        subprocess.run(["git", "add", "-f", rel], cwd=REPO, capture_output=True, check=True)
        r = subprocess.run([sys.executable, "scripts/preflight.py"],
                           capture_output=True, text=True, cwd=REPO, timeout=900)
        assert r.returncode != 0, f"preflight passed despite a planted {label}"
        assert probe.name in r.stdout, (
            f"preflight failed but did not name the offending file for {label}:\n{r.stdout}")
    finally:
        subprocess.run(["git", "rm", "-q", "--cached", "--ignore-unmatch", rel],
                       cwd=REPO, capture_output=True)
        probe.unlink(missing_ok=True)


def test_the_readme_diagram_is_present_and_renderable():
    """A mermaid block GitHub cannot parse renders as a wall of text on the front page."""
    text = README.read_text(encoding="utf-8")
    assert "```mermaid" in text, "the architecture diagram is missing"
    block = text.split("```mermaid", 1)[1].split("```", 1)[0]
    # Count KEYWORDS, not substrings: "end" also lives inside "Append-only", which made the
    # first version of this test report four ends against two subgraphs.
    lines = [ln.strip() for ln in block.splitlines()]
    opens = sum(1 for ln in lines if ln.startswith("subgraph "))
    closes = sum(1 for ln in lines if ln == "end")
    assert opens == closes, f"unbalanced mermaid: {opens} subgraph, {closes} end"
    assert opens >= 2, "expected the engine and model boundaries to be drawn as subgraphs"
    for must in ("Engine", "Copy gate", "ledger"):
        assert must.lower() in block.lower(), must
