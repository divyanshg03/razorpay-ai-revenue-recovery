"""Gate 0.7 sizing check. Regenerates every number quoted in docs/metric-definition.md.

Run: python scripts/gate_0_7_power.py
"""
import json
import math

Z_ALPHA = 1.959963985  # two-sided 0.05
Z_BETA = 0.841621234   # 80% power

SEED = 20260905
N = 5_000
ALLOC = {"A": 0.20, "B": 0.20, "C": 0.60}

# Sizing assumptions only. Not predictions, not results, not simulator inputs.
P = {"A": 0.08, "B": 0.18, "C": 0.25}

# Cost model, base (pre-GST) -> with 18% GST.
GST = 1.18
COST = {
    "whatsapp_utility_outside_window": 0.145,
    "whatsapp_utility_inside_window": 0.0,
    "sms_service_dlt": 0.18,
    "human_agent_connected_call": 13.00,
    "payment_link_notify": 0.0,
    "retry_attempt": 0.0,
}


def mde(p0, n0, p1, n1):
    se = math.sqrt(p0 * (1 - p0) / n0 + p1 * (1 - p1) / n1)
    return (Z_ALPHA + Z_BETA) * se


def main():
    n = {a: int(round(N * f)) for a, f in ALLOC.items()}
    assert sum(n.values()) == N, n

    out = {
        "seed": SEED,
        "cohort_size": N,
        "allocation": ALLOC,
        "arm_counts": n,
        "sizing_assumptions_recovery_rate": P,
        "power": {"alpha_two_sided": 0.05, "power": 0.80},
        "mde_pp": {
            "C_vs_B_primary": round(mde(P["B"], n["B"], P["C"], n["C"]) * 100, 2),
            "C_vs_A_secondary": round(mde(P["A"], n["A"], P["C"], n["C"]) * 100, 2),
        },
        "assumed_effect_pp": {
            "C_vs_B": round((P["C"] - P["B"]) * 100, 2),
            "C_vs_A": round((P["C"] - P["A"]) * 100, 2),
        },
        "cost_with_gst": {k: round(v * GST, 4) for k, v in COST.items()},
        "breakeven_uplift_pp_on_1000_rupee_debt": {
            k: round(v * GST / 1000 * 100, 4) for k, v in COST.items() if v > 0
        },
    }
    m = out["mde_pp"]
    e = out["assumed_effect_pp"]
    out["adequately_powered"] = {
        "C_vs_B_primary": e["C_vs_B"] > m["C_vs_B_primary"],
        "C_vs_A_secondary": e["C_vs_A"] > m["C_vs_A_secondary"],
    }
    print(json.dumps(out, indent=2))
    return out


if __name__ == "__main__":
    main()
