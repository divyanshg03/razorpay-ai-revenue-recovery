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
    assert {"readme-headline", "readme-failures", "readme-limitations",
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
    """A demo that re-implements the system is a demo of the demo."""
    src = (REPO / "scripts" / "demo.py").read_text(encoding="utf-8")
    for module in ("recovery.engine.machine", "recovery.engine.policy",
                   "recovery.llm.copy_gate", "recovery.llm.composer",
                   "recovery.llm.parser", "recovery.ledger.audit"):
        assert module in src, f"demo does not import {module}"


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
