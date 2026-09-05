# aml-alert-brief

Explainable, human-in-the-loop alert briefing for crypto AML teams.

Give it a structured alert. It gives the analyst back three things before they make a call:

1. **A plain-language brief.** What fired, who the customer is, what we already know about the counterparty and the history.
2. **An explainable risk read.** A score where every point has a named rule and a stated reason. If a factor cannot explain itself, it does not move the score.
3. **A first-draft SAR/STR narrative**, clearly marked as a draft that a human must verify, edit and own.

The tool reconstructs context. It does not make the decision.

Runs on the Python standard library. Ships with synthetic data only. MIT.

## Why

Most of an analyst's time on an alert is not the decision. It is rebuilding the context needed to decide: pulling the customer's history, working out why the rule fired, checking whether the counterparty label means anything, re-reading the support thread. The decision is usually quick once the picture is assembled.

This tool automates the assembly and leaves the judgment where it belongs.

## Design rules

* **Explainability is not optional.** Anything that touches a regulatory decision must have a reason a human can defend in front of a regulator. "The model said so" is not a rationale.
* **Attribution is a lead, not evidence.** Counterparty cluster labels raise the score, but the brief explicitly tells the analyst to corroborate them off-chain.
* **The customer's own words count.** Support-chat signals ("how do I split this so it is not flagged", "the money is not mine") are scored as first-class factors, because they usually are.
* **Nothing is filed automatically.** The narrative is a starting point. It carries a "do not file without review" line by design.
* **LLM is additive only.** The optional `--llm` step rewrites the brief for readability under a system prompt that forbids adding facts, removing factors or changing numbers. The score and the draft narrative never pass through the model.

## Quick start

```bash
git clone https://github.com/Niphridium/aml-alert-brief.git
cd aml-alert-brief
python3 src/triage.py --json
```

Output:

```
AL-2026-0001: HIGH    85/100  (5 factors)
AL-2026-0002: LOW      5/100  (2 factors)
AL-2026-0003: HIGH   100/100  (8 factors)

Wrote 3 brief(s) and draft(s) to ./out/
```

Each alert produces `<id>_brief.md` and `<id>_sar_draft.md`. With `--json`, a `scoring.json` with every factor and reason is written alongside. See [`examples/out/`](examples/out/) for the output on the bundled synthetic alerts.

### Optional LLM rewrite

```bash
pip install anthropic
export ANTHROPIC_API_KEY=...
python3 src/triage.py --llm
```

If the key or SDK is missing the tool falls back to the plain brief and says so. Set `AML_BRIEF_MODEL` to change the model.

## Input format

A JSON list of alerts. Field names are deliberately boring so you can map your own monitoring output onto them.

```json
{
  "alert_id": "AL-2026-0001",
  "customer":    {"id": "...", "kyc_level": "basic|full", "account_age_days": 9, "country": "KZ"},
  "trigger":     {"rule_id": "R-STRUCT-01", "rule_name": "...", "threshold_usd": 1000,
                  "window_hours": 24, "observed_count": 7, "observed_total_usd": 6740},
  "transaction": {"asset": "USDT", "chain": "TRON", "direction": "in|out", "amount_usd": 6740,
                  "counterparty_type": "external_wallet|exchange|...", "counterparty_label": null,
                  "hops_to_known_service": 2},
  "history":     {"prior_alerts_90d": 0, "avg_monthly_volume_usd": 300, "stated_source_of_funds": "salary"},
  "support_signals": ["asked in chat how to split a transfer so it is not flagged"],
  "tags": []
}
```

## Scoring factors

| Rule | Points | Fires when |
|---|---|---|
| `structuring_pattern` | +25 | Rule id starts with `R-STRUCT` |
| `third_party_funding` | +20 | Rule id starts with `R-3RDPARTY` |
| `velocity` | +15 | Rule id starts with `R-VELO` |
| `new_account` | +15 | Account younger than 30 days |
| `basic_kyc` | +10 | KYC level is basic |
| `volume_outlier` | +15 | Amount is more than 5x average monthly volume |
| `no_source_of_funds` | +10 | No stated source of funds |
| `repeat_alerts` | +10 | 2 or more alerts in 90 days |
| `high_risk_counterparty` | +20 | Counterparty label contains high-risk / mixer / darknet / sanction |
| `distant_from_known_service` | +5 | 3 or more hops to an attributed service |
| `intent_to_evade` | +20 | Support signal suggests avoiding thresholds |
| `admitted_third_party` | +20 | Support signal admits funds belong to someone else |
| `established_vip` | −10 | Fully verified VIP with account older than a year (mitigant) |

Bands: `LOW` < 40 ≤ `MEDIUM` < 70 ≤ `HIGH`.

The weights are illustrative. Tune them to your own risk appetite and validate against your own outcomes before relying on them. The point of the tool is that when you do change a weight, the reason is written down next to it.

## What this is not

* Not a transaction-monitoring engine. It sits downstream of one.
* Not a decisioning system. It never closes, escalates or files anything.
* Not trained on real data. Everything in `sample_data/` is synthetic.

## Related

[`support-chat-aml-triage`](https://github.com/Niphridium/support-chat-aml-triage): mining AML signal and macro backlogs out of customer-support chat exports. This tool consumes the kind of signal that one produces.

## License

MIT. See [LICENSE](LICENSE).
