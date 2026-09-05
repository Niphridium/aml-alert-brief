#!/usr/bin/env python3
"""
aml-alert-brief
===============

Turns a structured AML alert into three things an analyst actually needs
before making a decision:

  1. A plain-language brief   (what fired, who, what we already know)
  2. An explainable risk read  (every point on the score has a stated reason)
  3. A first-draft SAR/STR narrative, clearly labelled as a DRAFT

Design rules, on purpose:

  * The tool reconstructs context. It does not make the decision.
  * Every scoring factor is a named rule with a human-readable reason.
    If it cannot explain why, it does not get to move the score.
  * Nothing is filed automatically. The narrative is a draft for a
    human to verify, edit and own.
  * Optional LLM enrichment (--llm) is additive only: it rewrites the
    brief for readability and MUST NOT change the score or the facts.

Runs on the Python standard library. Synthetic data only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Explainable scoring
# ---------------------------------------------------------------------------

@dataclass
class Factor:
    """One contribution to the risk score, with the reason spelled out."""
    rule: str
    points: int
    reason: str


@dataclass
class RiskRead:
    score: int
    band: str
    factors: list[Factor] = field(default_factory=list)
    recommended_action: str = ""


def _band(score: int) -> str:
    if score >= 70:
        return "HIGH"
    if score >= 40:
        return "MEDIUM"
    return "LOW"


def _recommend(band: str) -> str:
    return {
        "HIGH":   "Escalate for EDD and SAR/STR review. Consider a temporary hold pending source-of-funds.",
        "MEDIUM": "Analyst review required. Request source-of-funds and check counterparty history before closing.",
        "LOW":    "Likely closable after a quick sanity check. Document the rationale.",
    }[band]


def score_alert(alert: dict[str, Any]) -> RiskRead:
    """
    Deterministic, rule-based scoring. Each factor states its reason.
    Weights are illustrative. Tune them to your own risk appetite and
    validate against your own outcomes before relying on them.
    """
    c = alert.get("customer", {})
    t = alert.get("trigger", {})
    tx = alert.get("transaction", {})
    h = alert.get("history", {})
    signals = alert.get("support_signals", []) or []
    tags = alert.get("tags", []) or []

    factors: list[Factor] = []

    # -- Rule type ---------------------------------------------------------
    rule_id = str(t.get("rule_id", ""))
    if rule_id.startswith("R-STRUCT"):
        factors.append(Factor("structuring_pattern", 25,
            f"{t.get('observed_count', '?')} deposits under the {t.get('threshold_usd')} USD "
            f"threshold inside {t.get('window_hours')}h (total {t.get('observed_total_usd')} USD). "
            "This is the classic structuring shape."))
    elif rule_id.startswith("R-3RDPARTY"):
        factors.append(Factor("third_party_funding", 20,
            "Rule indicates funds may not belong to the account holder."))
    elif rule_id.startswith("R-VELO"):
        factors.append(Factor("velocity", 15,
            f"Large movement ({t.get('observed_total_usd')} USD) inside a {t.get('window_hours')}h window."))

    # -- Customer profile --------------------------------------------------
    age = int(c.get("account_age_days", 0) or 0)
    if age < 30:
        factors.append(Factor("new_account", 15,
            f"Account is only {age} days old. New accounts with immediate activity carry more risk."))

    if str(c.get("kyc_level", "")).lower() == "basic":
        factors.append(Factor("basic_kyc", 10,
            "Customer is on basic KYC only. Identity assurance is limited."))

    # -- Volume vs. stated profile ----------------------------------------
    avg = float(h.get("avg_monthly_volume_usd", 0) or 0)
    amt = float(tx.get("amount_usd", 0) or 0)
    if avg > 0 and amt > avg * 5:
        factors.append(Factor("volume_outlier", 15,
            f"Transaction ({amt:,.0f} USD) is more than 5x the customer's average monthly volume "
            f"({avg:,.0f} USD)."))

    sof = str(h.get("stated_source_of_funds", "")).lower()
    if sof in ("", "not provided", "unknown"):
        factors.append(Factor("no_source_of_funds", 10,
            "No stated source of funds on file."))

    # -- History -----------------------------------------------------------
    prior = int(h.get("prior_alerts_90d", 0) or 0)
    if prior >= 2:
        factors.append(Factor("repeat_alerts", 10,
            f"{prior} prior alerts in the last 90 days."))

    # -- Counterparty / on-chain -----------------------------------------
    label = str(tx.get("counterparty_label") or "").lower()
    if "high-risk" in label or "mixer" in label or "darknet" in label or "sanction" in label:
        factors.append(Factor("high_risk_counterparty", 20,
            f"Counterparty attributed as: {tx.get('counterparty_label')}. "
            "Attribution is a lead, not a verdict. Corroborate before relying on it."))

    hops = int(tx.get("hops_to_known_service", 0) or 0)
    if hops >= 3:
        factors.append(Factor("distant_from_known_service", 5,
            f"{hops} hops to the nearest attributed service. Harder to explain the path."))

    # -- Support / behavioural signals ------------------------------------
    for s in signals:
        s_low = s.lower()
        if any(k in s_low for k in ("split", "not flagged", "avoid", "under the limit")):
            factors.append(Factor("intent_to_evade", 20,
                f"Customer's own words in support: \"{s}\"."))
        elif any(k in s_low for k in ("not mine", "for a friend", "someone else", "on behalf")):
            factors.append(Factor("admitted_third_party", 20,
                f"Customer's own words in support: \"{s}\"."))

    # -- Mitigants ---------------------------------------------------------
    if "vip" in [x.lower() for x in tags] and str(c.get("kyc_level", "")).lower() == "full" and age > 365:
        factors.append(Factor("established_vip", -10,
            "Established, fully verified VIP with a long history. Mitigant, not a pass."))

    score = max(0, min(100, sum(f.points for f in factors)))
    band = _band(score)
    return RiskRead(score=score, band=band, factors=factors, recommended_action=_recommend(band))


# ---------------------------------------------------------------------------
# Brief and draft narrative
# ---------------------------------------------------------------------------

def build_brief(alert: dict[str, Any], read: RiskRead) -> str:
    c, t, tx, h = (alert.get(k, {}) for k in ("customer", "trigger", "transaction", "history"))
    lines = [
        f"# Alert brief: {alert.get('alert_id')}",
        "",
        f"**Risk read:** {read.band} ({read.score}/100)",
        f"**Recommended next step:** {read.recommended_action}",
        "",
        "## What fired",
        f"- Rule: `{t.get('rule_id')}` {t.get('rule_name')}",
        f"- Observed: {t.get('observed_total_usd')} USD"
        + (f" across {t['observed_count']} transactions" if t.get("observed_count") else "")
        + (f" within {t['window_hours']}h" if t.get("window_hours") else ""),
        "",
        "## Who",
        f"- Customer `{c.get('id')}`, country {c.get('country')}, KYC level **{c.get('kyc_level')}**, "
        f"account age {c.get('account_age_days')} days",
        f"- Stated source of funds: {h.get('stated_source_of_funds') or 'not provided'}",
        f"- Average monthly volume: {h.get('avg_monthly_volume_usd')} USD; prior alerts (90d): {h.get('prior_alerts_90d')}",
        "",
        "## Transaction",
        f"- {tx.get('direction', '?').upper()} {tx.get('amount_usd')} USD in {tx.get('asset')} on {tx.get('chain')}",
        f"- Counterparty: {tx.get('counterparty_type')}"
        + (f" attributed as *{tx['counterparty_label']}*" if tx.get("counterparty_label") else " (unattributed)"),
        f"- Hops to nearest known service: {tx.get('hops_to_known_service')}",
        "",
        "## Why the score is what it is",
    ]
    if not read.factors:
        lines.append("- No scoring factors triggered.")
    for f in read.factors:
        sign = "+" if f.points >= 0 else ""
        lines.append(f"- **{sign}{f.points}** `{f.rule}`: {f.reason}")

    sig = alert.get("support_signals") or []
    if sig:
        lines += ["", "## Support signals (customer's own words)"]
        lines += [f"- \"{s}\"" for s in sig]

    lines += [
        "",
        "## Open questions for the analyst",
        "- Does the stated source of funds explain this activity?",
        "- Is the counterparty attribution corroborated by anything off-chain?",
        "- Is there a benign explanation consistent with the customer's history?",
    ]
    return "\n".join(lines)


def build_sar_draft(alert: dict[str, Any], read: RiskRead) -> str:
    """
    First-pass narrative. Deliberately plain and factual. It is a DRAFT:
    a human must verify every statement, add case-specific detail, and
    decide whether filing is warranted at all.
    """
    c, t, tx, h = (alert.get(k, {}) for k in ("customer", "trigger", "transaction", "history"))
    reasons = "; ".join(f.reason.rstrip(".") for f in read.factors if f.points > 0)
    return "\n".join([
        "# DRAFT SAR/STR narrative (for human review, not for filing)",
        "",
        f"On review of alert {alert.get('alert_id')}, customer {c.get('id')} (KYC level: {c.get('kyc_level')}, "
        f"account opened {c.get('account_age_days')} days prior) was observed conducting "
        f"{tx.get('direction')}bound activity of approximately {tx.get('amount_usd')} USD in {tx.get('asset')} "
        f"on the {tx.get('chain')} network. The activity triggered monitoring rule {t.get('rule_id')} "
        f"({t.get('rule_name')}).",
        "",
        f"Factors considered: {reasons}.",
        "",
        f"The customer's stated source of funds is \"{h.get('stated_source_of_funds') or 'not provided'}\". "
        f"Average monthly volume on the account is approximately {h.get('avg_monthly_volume_usd')} USD, "
        f"with {h.get('prior_alerts_90d')} prior alert(s) in the preceding 90 days.",
        "",
        "[ANALYST TO COMPLETE: outcome of source-of-funds request, corroboration of counterparty "
        "attribution, any customer explanation obtained, and the basis for the filing decision.]",
        "",
        "_This narrative was generated as a starting point from structured alert data. "
        "It has not been reviewed. Do not file without human verification._",
    ])


# ---------------------------------------------------------------------------
# Optional LLM enrichment (additive only)
# ---------------------------------------------------------------------------

def llm_rewrite_brief(brief: str) -> str:
    """
    Rewrites the brief for readability using the Anthropic API.
    Guardrails: the model is told it may not add facts, remove factors,
    or change the score. If the SDK or key is missing, the original
    brief is returned unchanged.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.stderr.write("[llm] ANTHROPIC_API_KEY not set; returning original brief.\n")
        return brief
    try:
        import anthropic  # type: ignore
    except ImportError:
        sys.stderr.write("[llm] anthropic SDK not installed; returning original brief.\n")
        return brief

    client = anthropic.Anthropic(api_key=api_key)
    system = (
        "You are editing an AML alert brief for an analyst. Improve clarity and flow only. "
        "You must not add facts, remove any scoring factor, change any number, or change the "
        "risk band or recommended action. Keep the markdown structure. Output the brief only."
    )
    msg = client.messages.create(
        model=os.environ.get("AML_BRIEF_MODEL", "claude-sonnet-4-5"),
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": brief}],
    )
    return "".join(getattr(b, "text", "") for b in msg.content) or brief


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Explainable AML alert brief + draft SAR narrative.")
    p.add_argument("--input", default="sample_data/alerts.json", help="Path to alerts JSON (list).")
    p.add_argument("--out", default="out", help="Output directory.")
    p.add_argument("--llm", action="store_true", help="Rewrite briefs for readability via Anthropic API.")
    p.add_argument("--json", action="store_true", help="Also emit machine-readable scoring JSON.")
    args = p.parse_args(argv)

    alerts = json.loads(Path(args.input).read_text(encoding="utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    summary = []
    for a in alerts:
        read = score_alert(a)
        brief = build_brief(a, read)
        if args.llm:
            brief = llm_rewrite_brief(brief)
        sar = build_sar_draft(a, read)

        aid = a.get("alert_id", "alert")
        (out / f"{aid}_brief.md").write_text(brief, encoding="utf-8")
        (out / f"{aid}_sar_draft.md").write_text(sar, encoding="utf-8")
        summary.append({
            "alert_id": aid, "score": read.score, "band": read.band,
            "factors": [{"rule": f.rule, "points": f.points, "reason": f.reason} for f in read.factors],
            "recommended_action": read.recommended_action,
        })
        print(f"{aid}: {read.band:<6} {read.score:>3}/100  ({len(read.factors)} factors)")

    if args.json:
        (out / "scoring.json").write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "alerts": summary,
        }, indent=2), encoding="utf-8")

    print(f"\nWrote {len(alerts)} brief(s) and draft(s) to ./{out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
