"""Causal daily research replay and inspectable account artifacts."""

from __future__ import annotations

import csv
import hashlib
import html
import json
from collections import defaultdict
from pathlib import Path

from quant_data_kit.derivatives.models import decimal

from .derivatives import DerivativeAccount


def replay(bundle, config, decide):
    """Decide after an observed bar; execute at the NEXT bar's modeled open.

    ``decide`` receives only the latest synchronized, available quotes and time.
    Fees and adverse tick slippage apply to every fill. Daily-bar fills are a
    declared research assumption, not bid/ask or actual execution evidence.
    """
    groups = defaultdict(dict)
    for q in bundle.quotes:
        groups[q.session][q.instrument_id] = q
    sessions = sorted(groups)
    start, end = config.get("start") or sessions[0], config.get("end") or sessions[-1]
    sessions = [s for s in sessions if start <= s <= end]
    if len(sessions) < 3:
        raise ValueError("at least three observed sessions required")
    opened = min(q.at for q in groups[sessions[0]].values())
    account = DerivativeAccount(
        bundle.contracts,
        config["initial_cash"],
        opened,
        fee_per_contract=config.get("fee_per_contract", 0),
        slippage_ticks=config.get("slippage_ticks", 0),
    )
    nav = [
        {
            "date": sessions[0],
            "nav": str(config["initial_cash"]),
            "cash": str(config["initial_cash"]),
            "initial_margin": "0",
            "maintenance_margin": "0",
            "margin_breach": False,
        }
    ]
    positions, decisions = [], []
    pending, decision_at = {}, None
    for index, session in enumerate(sessions):
        observed = groups[session]
        stamps = {q.at for q in observed.values()}
        if len(stamps) != 1:
            raise ValueError(
                "daily basket replay needs synchronized observations; normalize session timestamps"
            )
        fill_at = next(iter(stamps))
        available = max(q.available_at for q in observed.values())
        if index:
            if decision_at >= fill_at:
                raise ValueError("next bar opens before the previous decision becomes available")
            target = {} if index == len(sessions) - 1 else pending
            legs = []
            active_ids = {i for i, q in account.snapshot()["positions"].items() if decimal(q) != 0}
            for instrument_id in sorted(active_ids | set(target)):
                c = bundle.contract(instrument_id)
                if c.expiry <= fill_at:
                    q = observed.get(instrument_id)
                    if (
                        account.quantity(instrument_id)
                        and c.kind == "option"
                        and c.settlement == "cash"
                    ):
                        if q is None or q.at != c.expiry or q.underlying_price is None:
                            raise ValueError(
                                "cash expiry requires an explicit underlying fixing at exact expiry"
                            )
                        continue
                    if account.quantity(instrument_id):
                        raise ValueError(
                            "position reached expiry without supported fixing; close or roll earlier"
                        )
                    continue
                delta = decimal(target.get(instrument_id, 0)) - account.quantity(instrument_id)
                if delta:
                    q = observed.get(instrument_id)
                    if q is None or q.volume <= 0 or q.open <= 0:
                        raise ValueError(
                            f"missing positive, liquid next-bar fill for {instrument_id}"
                        )
                    legs.append((instrument_id, delta, q.open))
            if legs:
                account.trade_batch(legs, fill_at, f"basket:{session}")
        for instrument_id, qty in account.snapshot()["positions"].items():
            if decimal(qty) == 0:
                continue
            c, q = bundle.contract(instrument_id), observed.get(instrument_id)
            if q is None:
                raise ValueError(f"missing held-position valuation for {instrument_id}")
            if c.kind == "option" and available >= c.expiry:
                if c.settlement != "cash" or q.at != c.expiry or q.underlying_price is None:
                    raise ValueError(
                        "expiry replay requires a cash contract and exact underlying fixing; otherwise close earlier"
                    )
                account.exercise(
                    instrument_id,
                    q.underlying_price,
                    available,
                    f"expiry:{session}:{instrument_id}",
                )
            elif c.kind == "future":
                if q.settlement is None or q.settlement <= 0:
                    raise ValueError(
                        "daily futures replay requires explicit positive settlement prices"
                    )
                account.settle_future(
                    instrument_id, q.settlement, available, f"settle:{session}:{instrument_id}"
                )
            else:
                account.mark(instrument_id, q.close, available, f"mark:{session}:{instrument_id}")
        snap = account.snapshot()
        if snap["margin_breach"]:
            raise ValueError(
                "declared maintenance margin breached; no fictitious automatic liquidation"
            )
        if index:
            nav.append(
                {
                    k: v
                    for k, v in {"date": session, **snap}.items()
                    if k
                    in {
                        "date",
                        "nav",
                        "cash",
                        "initial_margin",
                        "maintenance_margin",
                        "margin_breach",
                    }
                }
            )
        positions.extend(
            {"date": session, "instrument_id": i, "quantity": qty}
            for i, qty in snap["positions"].items()
        )
        eligible = {
            i: q
            for i, q in observed.items()
            if bundle.contract(i).known_at <= available < bundle.contract(i).last_trade_at
        }
        pending = decide(eligible, available) if index < len(sessions) - 1 else {}
        if not isinstance(pending, dict) or any(i not in eligible for i in pending):
            raise ValueError("decision selected a contract unavailable at decision time")
        decisions.append(
            {
                "decision_at": available.isoformat(),
                "session": session,
                "targets": {i: str(decimal(v)) for i, v in pending.items()},
            }
        )
        decision_at = available
    account.validate_balance()
    bundle.verify_unchanged()
    return {
        "nav": nav,
        "positions": positions,
        "fills": account.fills,
        "lifecycle": account.lifecycle,
        "decisions": decisions,
        "ledger": [
            {
                "transaction_id": tx.transaction_id,
                "reference_id": tx.reference_id,
                "at": tx.event_time.isoformat(),
                "ledger_account": p.ledger_account,
                "currency": p.currency,
                "amount": str(p.amount.to_decimal()),
                "instrument_id": p.instrument_id or "",
            }
            for tx in account.ledger.transactions
            for p in tx.postings
        ],
        "final_account": account.snapshot(),
    }


def write_csv(path, rows, columns=None):
    columns = columns or (list(rows[0]) if rows else ["empty"])
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, columns)
        writer.writeheader()
        writer.writerows(rows)


def write_artifacts(output, study, tables):
    """Small responsive report; no scripts, no external requests or raw HTML inputs."""
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(
            "research output must be new or empty; existing reports are immutable"
        )
    output.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        if Path(name).name != name or not name.endswith(".csv"):
            raise ValueError("artifact table must be a plain CSV filename")
        write_csv(output / name, rows)
    (output / "study.json").write_text(
        json.dumps(study, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    esc = lambda v: html.escape(str(v))
    nav = tables.get("nav.csv", [])
    chart = ""
    if nav:
        values = [float(row["nav"]) for row in nav]
        lo, hi = min(values), max(values)
        points = " ".join(
            f"{20 + 760 * i / max(1, len(values) - 1):.1f},{180 - 150 * (v - lo) / max(1e-9, hi - lo):.1f}"
            for i, v in enumerate(values)
        )
        chart = f'<h2>账户净值</h2><p>{esc(nav[0]["date"])} → {esc(nav[-1]["date"])} · {esc(values[-1])} {esc(study.get("currency", ""))}</p><svg role="img" aria-label="账户净值曲线" viewBox="0 0 800 210"><polyline fill="none" stroke="#55d9ad" stroke-width="3" points="{points}"/></svg>'
    sections = []
    for name, rows in tables.items():
        if not rows:
            continue
        columns = list(rows[0])
        header = "".join(f"<th>{esc(k)}</th>" for k in columns)
        body = "".join(
            "<tr>" + "".join(f"<td>{esc(row.get(k, ''))}</td>" for k in columns) + "</tr>"
            for row in rows[:100]
        )
        sections.append(
            f'<details><summary>{esc(name)} · {len(rows)} 行</summary><div class="scroll"><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div><p>预览前 100 行；完整数据见同名 CSV。</p></details>'
        )
    summary = "".join(
        f"<p><b>{esc(k)}</b>：{esc(v)}</p>"
        for k, v in study.items()
        if k not in {"config", "decisions"}
    )
    page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(study["title"])}</title>
<style>body{{background:#10202c;color:#e5f0f5;font:16px system-ui;margin:0 auto;padding:24px;max-width:1100px}}h1{{font-size:28px}}p{{line-height:1.6;overflow-wrap:anywhere}}b{{color:#7de5c6}}svg{{width:100%;background:#152e3c;border-radius:12px}}details{{margin:16px 0;background:#18303d;padding:16px;border-radius:10px}}summary{{cursor:pointer}}.scroll{{overflow:auto}}td,th{{padding:8px;border-bottom:1px solid #36505b;text-align:left;white-space:nowrap}}.badge{{background:#654418;padding:12px;border-radius:8px}}</style>
<h1>{esc(study["title"])}</h1><p class="badge">研究用途 · {esc(study["evidence_kind"])} · 不发送真实订单</p>{chart}{summary}{"".join(sections)}</html>"""
    (output / "report.html").write_text(page, encoding="utf-8")
    manifest = {
        "schema": "quant.derivative-artifacts/v1",
        "input_sha256": study["input_sha256"],
        "files": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(output.iterdir())
            if p.is_file()
        },
    }
    (output / "artifacts.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def verify_artifacts(output):
    output = Path(output).resolve()
    manifest = json.loads((output / "artifacts.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "quant.derivative-artifacts/v1":
        raise ValueError("unsupported artifact manifest")
    expected = manifest.get("files", {})
    if not {"report.html", "study.json"} <= set(expected):
        raise ValueError("missing research artifacts")
    for name, sha in expected.items():
        path = (output / name).resolve()
        if (
            path.parent != output
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != sha
        ):
            raise ValueError(f"changed or missing artifact: {name}")
    if (output / "ledger.csv").is_file():
        totals = defaultdict(lambda: decimal(0))
        with (output / "ledger.csv").open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                totals[(row["transaction_id"], row["currency"])] += decimal(row["amount"])
        if any(totals.values()):
            raise ValueError("ledger transaction does not balance")
    return {
        "status": "passed",
        "files": len(expected),
        "input_sha256": manifest["input_sha256"],
        "scope": "artifact hashes and per-currency ledger balance; rerun against pinned code/input for independent reproduction",
    }
