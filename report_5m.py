"""Relatório determinístico do paper/live trade a partir do ledger e do journal.

Uso: python report_5m.py [DATA_DIR] [--vs nome=DIR ...] [--telegram]
  --vs        compara com outros motores (shadows), ex.: --vs alt=data-shadow-alt
  --telegram  manda a calibração para o Telegram configurado no .env
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from src import calibration


def fmt_ts(ts) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%m-%d %H:%M")


def main(data_dir: str = "data", compare=None) -> None:
    d = Path(data_dir)
    # Só leitura (sqlite mode=ro): o relatório roda ao lado do motor sem tocar no ledger dele.
    rows = calibration.load_rows(d / "ledger.sqlite")
    events = calibration.load_events(d / "journal.jsonl")

    by_status = Counter(r["status"] for r in rows)
    decisions = [e for e in events if e["event"] == "decision"]
    evals = [e for e in events if e["event"] == "eval"]
    settled = [r for r in rows if r["status"] == "settled"]
    filled_or_settled = [r for r in rows if r["status"] in ("filled", "settled")]
    quotes = [e for e in events if e["event"] == "order_posted"]
    fills = [e for e in events if e["event"] == "fill"]
    jev_err = [e for e in events if e["event"] == "jev_error"]

    print("=== JEV BTC 5m | relatório ===")
    print(f"janelas vistas: {len(rows)} | por status: {dict(by_status)}")
    print(f"avaliações no journal: {len(evals)} | decisões (chamadas ao Jev): {len(decisions)} | erros Jev: {len(jev_err)}")
    if decisions:
        lat = sorted(e.get("jev_latency_ms", 0) for e in decisions)
        print(f"latência Jev ms: mediana {lat[len(lat)//2]} | p90 {lat[int(len(lat)*0.9)]}")
        vet = Counter((e.get("vetoed") or "ordem").split(" ")[0] for e in decisions)
        print(f"resultado das decisões: {dict(vet)}")
    print(f"ordens postadas: {len(quotes)} | fills: {len(fills)} | taxa de fill: {(len(fills)/len(quotes)*100 if quotes else 0):.0f}%")

    if settled:
        pnl = sum(r["pnl_usd"] or 0 for r in settled)
        cost = sum(r["cost_usd"] or 0 for r in settled)
        wins = sum(1 for r in settled if (r["pnl_usd"] or 0) > 0)
        brier = [((r["p_model"] or 0.5) - (1.0 if r["outcome"] == "Up" else 0.0)) ** 2 for r in settled if r.get("p_model") is not None]
        # p_model é P(Up); edge realizado por trade = payout − custo, por dólar apostado
        print(f"liquidadas: {len(settled)} | acertos: {wins} ({wins/len(settled)*100:.0f}%) | PnL: {pnl:+.2f} USD sobre {cost:.2f} apostados ({(pnl/cost*100 if cost else 0):+.1f}%)")
        if brier:
            print(f"Brier do p_modelo no momento da ordem: {sum(brier)/len(brier):.3f} (0,25 = chute)")
        print("\n  janela        lado  limite  qtd    p_mod  resultado  pnl")
        for r in settled[-25:]:
            print(f"  {fmt_ts(r['ts'])}  {r['side']:<4}  {r['fill_price'] or 0:.2f}   {r['filled_shares'] or 0:>5.2f}  {r['p_model'] or 0:.3f}  {r['outcome']:<9}  {r['pnl_usd'] or 0:+.2f}")
    else:
        print("liquidadas: 0")

    print()
    print(calibration.render(d, compare))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    vs, send = {}, "--telegram" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--vs":
            for item in sys.argv[i + 1:]:
                if item.startswith("--"):
                    break
                name, _, path = item.partition("=")
                vs[name] = Path(path)
    args = [a for a in args if "=" not in a]
    data = args[0] if args else "data"
    main(data, vs)
    if send:
        import os

        from dotenv import load_dotenv

        from src import notify

        load_dotenv(".env")
        n = notify.from_env(os.environ, prefix="[JEV 5m] ")
        if n.enabled:
            n.send("report", "🔬 Calibração (pedido manual)", 0, mono=calibration.render(Path(data), vs))
            import time

            time.sleep(3)  # deixa a thread do notificador enviar antes de sair
