"""Portfolio divergence rate — deterministic rollup of divergence verdicts.

bench method contract: JSON {"params", "inputs"} on stdin -> {"rows": [...]}
on stdout. Pure function, stdlib only.
"""
import json
import sys


def main() -> None:
    payload = json.load(sys.stdin)
    params = payload.get("params") or {}
    verdicts = payload.get("inputs", {}).get("findings:divergence_verdict", [])

    status_filter = params.get("status", "all")
    if status_filter != "all":
        verdicts = [v for v in verdicts if v.get("status") == status_filter]
    if params.get("peril"):
        verdicts = [v for v in verdicts if v.get("peril") == params["peril"]]

    by_peril: dict = {}
    for v in verdicts:
        peril = v.get("peril", "unknown")
        slot = by_peril.setdefault(
            peril,
            {
                "peril": peril,
                "total": 0,
                "agree": 0,
                "diverge_signal_higher": 0,
                "diverge_reference_higher": 0,
                "insufficient_data": 0,
                "reason_codes": {},
            },
        )
        slot["total"] += 1
        verdict = v.get("verdict", "unknown")
        if verdict in slot:
            slot[verdict] += 1
        code = v.get("reason_code")
        if code:
            slot["reason_codes"][code] = slot["reason_codes"].get(code, 0) + 1

    rows = []
    for peril in sorted(by_peril):
        s = by_peril[peril]
        diverging = s["diverge_signal_higher"] + s["diverge_reference_higher"]
        assessed = s["total"] - s["insufficient_data"]
        top_code = max(s["reason_codes"].items(), key=lambda kv: kv[1])[0] if s["reason_codes"] else ""
        rows.append(
            {
                "peril": peril,
                "verdicts_total": s["total"],
                "agree": s["agree"],
                "diverge_signal_higher": s["diverge_signal_higher"],
                "diverge_reference_higher": s["diverge_reference_higher"],
                "insufficient_data": s["insufficient_data"],
                "divergence_rate_pct": round(100 * diverging / assessed, 1) if assessed else 0.0,
                "dominant_reason_code": top_code,
                "status_filter": status_filter,
            }
        )
    json.dump({"rows": rows}, sys.stdout)


if __name__ == "__main__":
    main()
