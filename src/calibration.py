"""Leitura do journal: calibração (Brier por faixa de probabilidade, fase da janela e regime do Jev),
modelo contra mercado, efeito dos vetos, execução, taker hipotético, σ realizada, evidência de saída
antecipada e comparação live × shadows. Determinístico, só leitura."""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REGIME_NAMES = {0: "chop", 1: "misto", 2: "tendência"}
PHASE_BANDS = ((30, 90), (90, 150), (150, 210), (210, 276))
P_EDGES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0001)
WINDOW_S = 300


# ------------------------------------------------------------------ carga
def load_events(journal_path: Path) -> List[dict]:
    out: List[dict] = []
    if not Path(journal_path).exists():
        return out
    with Path(journal_path).open(encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def load_rows(db_path: Path) -> List[dict]:
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM windows ORDER BY ts")]
    finally:
        conn.close()


def outcomes_by_ts(rows: Iterable[dict], events: Iterable[dict]) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for e in events:
        if e.get("event") in ("outcome", "settled") and e.get("outcome") in ("Up", "Down"):
            out[int(e["ts"])] = e["outcome"]
    for r in rows:  # o ledger vence: carrega a correção da reconciliação oficial
        if r.get("outcome") in ("Up", "Down"):
            out[int(r["ts"])] = r["outcome"]
    return out


# ------------------------------------------------------------------ métricas
def brier(samples: Sequence[Tuple[float, int]]) -> Optional[float]:
    return sum((p - y) ** 2 for p, y in samples) / len(samples) if samples else None


def _fmt(v: Optional[float], spec: str = ".3f") -> str:
    return "  —  " if v is None else format(v, spec)


def _evals_with_outcome(events: Iterable[dict], outcome: Dict[int, str], kinds=("eval", "decision")) -> List[dict]:
    out = []
    for e in events:
        if e.get("event") in kinds and e.get("p_raw") is not None and int(e.get("ts", 0)) in outcome:
            out.append({**e, "y": 1 if outcome[int(e["ts"])] == "Up" else 0})
    return out


def _mid(e: dict) -> Optional[float]:
    b, a = e.get("up_bid"), e.get("up_ask")
    return (b + a) / 2 if b is not None and a is not None else None


def section_probability(evals: List[dict]) -> List[str]:
    lines = ["Calibração do p_raw por faixa (P(Up) do modelo × Up de fato)", "  faixa        aval  janelas  p médio  Up real  Brier"]
    for lo, hi in zip(P_EDGES, P_EDGES[1:]):
        b = [e for e in evals if lo <= e["p_raw"] < hi]
        if not b:
            continue
        n_win = len({e["ts"] for e in b})
        lines.append(
            f"  [{lo:.1f},{min(hi, 1.0):.1f})   {len(b):>5}  {n_win:>7}  {sum(e['p_raw'] for e in b) / len(b):>7.3f}  "
            f"{sum(e['y'] for e in b) / len(b) * 100:>6.0f}%  {_fmt(brier([(e['p_raw'], e['y']) for e in b]))}"
        )
    return lines


def section_model_vs_market(evals: List[dict]) -> List[str]:
    both = [(e["p_raw"], _mid(e), e["y"]) for e in evals if _mid(e) is not None]
    if not both:
        return []
    bm = brier([(p, y) for p, _, y in both])
    bk = brier([(m, y) for _, m, y in both])
    verdict = "modelo melhor que o mercado" if bm < bk else "mercado melhor que o modelo"
    return [f"Modelo × mercado (n={len(both)}): Brier p_raw {_fmt(bm)} | Brier do mid do book {_fmt(bk)} | chute 0.250 → {verdict}"]


def section_phase(evals: List[dict]) -> List[str]:
    lines = ["Por fase da janela (segundos desde a abertura)", "  fase        aval  Brier   acerto do lado favorecido"]
    for lo, hi in PHASE_BANDS:
        b = [e for e in evals if lo <= int(e.get("phase", -1)) < hi]
        if not b:
            continue
        hit = sum(1 for e in b if (e["p_raw"] >= 0.5) == (e["y"] == 1)) / len(b)
        lines.append(f"  {lo:>3}-{hi - 1:<3}s   {len(b):>5}  {_fmt(brier([(e['p_raw'], e['y']) for e in b]))}  {hit * 100:>5.0f}%")
    return lines


def _regime(e: dict) -> Optional[int]:
    probs = (e.get("jev") or {}).get("regime_probs") or {}
    if not probs:
        return None
    return int(max(probs, key=lambda k: float(probs[k])))


def section_regime(decisions: List[dict]) -> List[str]:
    lines = ["Por regime do Jev (decisões com resultado conhecido)", "  regime       n   Brier p_raw  Brier p_adj  Brier direção Jev"]
    for k, name in REGIME_NAMES.items():
        b = [e for e in decisions if _regime(e) == k]
        if not b:
            continue
        adj = [(e["p_adj"], e["y"]) for e in b if e.get("p_adj") is not None]
        dire = [(float(e["jev"]["direction_p_up"]), e["y"]) for e in b if (e.get("jev") or {}).get("direction_p_up") is not None]
        lines.append(f"  {name:<10} {len(b):>4}   {_fmt(brier([(e['p_raw'], e['y']) for e in b])):>10}  {_fmt(brier(adj)):>11}  {_fmt(brier(dire)):>10}")
    return lines


def section_veto(decisions: List[dict]) -> List[str]:
    """O veto acerta? Compara o lado pretendido nas decisões liberadas e nas vetadas."""
    lines = []
    for label, pick in (("liberadas", lambda e: not e.get("vetoed")), ("vetadas pelo Jev", lambda e: str(e.get("vetoed") or "").startswith(("Jev", "anomalia")))):
        b = [e for e in decisions if pick(e) and e.get("side") in ("Up", "Down") and e.get("limit") is not None]
        if not b:
            continue
        win = [1 if (e["side"] == "Up") == (e["y"] == 1) else 0 for e in b]
        realized = sum(w - e["limit"] for w, e in zip(win, b)) / len(b)
        lines.append(f"  {label:<17} n={len(b):<4} lado venceu {sum(win) / len(b) * 100:>4.0f}%  edge realizado por share {realized:+.3f}")
    return ["Efeito do portão (lado pretendido × resultado)"] + lines if lines else []


def section_execution(events: List[dict]) -> List[str]:
    posted = [e for e in events if e.get("event") == "order_posted"]
    takers = sum(1 for e in posted if e.get("kind") == "taker")
    killed = sum(1 for e in events if e.get("event") == "taker_killed")
    fills = {e.get("order_id"): e for e in events if e.get("event") == "fill"}
    errs = [e for e in events if e.get("event") == "order_error"]
    kind = lambda e: e.get("kind") or ("cross" if "crosses book" in str(e.get("error")) else "region" if "region" in str(e.get("error")) else "other")
    n_cross, n_region = sum(1 for e in errs if kind(e) == "cross"), sum(1 for e in errs if kind(e) == "region")
    ttl = sum(1 for e in events if e.get("event") == "cancel_ttl")
    orphans = sum(1 for e in events if e.get("event") == "cancel_unconfirmed")
    lines = [
        "Execução",
        f"  postadas {len(posted)} | fills {len(fills)} ({(len(fills) / len(posted) * 100 if posted else 0):.0f}%) | canceladas por TTL {ttl} | "
        f"rejeitadas por cruzar o book {n_cross} | recusa de região {n_region} | outros erros {len(errs) - n_cross - n_region} | sem confirmação {orphans}",
    ]
    if takers:
        lines.append(f"  entradas taker: {takers} postadas | {takers - killed} executadas")
    post_ms = [e["post_ms"] for e in posted if e.get("post_ms") is not None]
    ttf = [fills[e["order_id"]]["t"] - e["t"] for e in posted if e.get("order_id") in fills]
    if post_ms:
        lines.append(f"  latência do post: mediana {median(post_ms):.0f} ms | máx {max(post_ms):.0f} ms")
    if ttf:
        lines.append(f"  tempo até o fill: mediana {median(ttf):.1f} s | máx {max(ttf):.1f} s")
    return lines


def section_taker(evals: List[dict], min_edge: float = 0.03) -> List[str]:
    b = [e for e in evals if e.get("cand_edge_taker") is not None and e["cand_edge_taker"] >= min_edge and e.get("cand_side")]
    if not b:
        return []
    real = []
    for e in b:
        p_side = e["p_raw"] if e["cand_side"] == "Up" else 1 - e["p_raw"]
        cost = p_side - e["cand_edge_taker"]  # ask + taxa taker
        real.append((1 if (e["cand_side"] == "Up") == (e["y"] == 1) else 0) - cost)
    return [f"Taker hipotético (edge taker previsto ≥ {min_edge:.2f}, já com a taxa): n={len(b)} | previsto {sum(e['cand_edge_taker'] for e in b) / len(b):+.3f} | realizado {sum(real) / len(real):+.3f} por share"]


def section_sigma(rows: List[dict], prior_1s: float) -> List[str]:
    rets = [math.log(r["close_price"] / r["strike"]) for r in rows
            if r.get("strike") and r.get("close_price") and 0.5 < (r["close_price"] / r["strike"]) < 2.0]
    if len(rets) < 10:
        return []
    s5 = math.sqrt(sum(x * x for x in rets) / len(rets))
    return [f"σ realizada (n={len(rets)} janelas): 5 min {s5:.5f} → σ_1s {s5 / math.sqrt(WINDOW_S):.2e} | prior em uso {prior_1s:.2e} ({(s5 / math.sqrt(WINDOW_S)) / prior_1s:.2f}× o prior)"]


def section_early_exit(rows: List[dict], events: List[dict], trigger: float = 0.35, fee_rate: float = 0.07) -> List[str]:
    """Contrafactual de saída antecipada: vender no bid na primeira marcação em que o modelo dá menos de
    `trigger` para o nosso lado, contra segurar até o fim."""
    marks: Dict[int, List[dict]] = {}
    for e in events:
        if e.get("event") == "mark":
            marks.setdefault(int(e["ts"]), []).append(e)
    real = [r for r in rows if r.get("status") == "closed"]
    held = exited = 0.0
    n = hit = 0
    for r in rows:
        if r.get("status") != "settled" or int(r["ts"]) not in marks:
            continue
        n += 1
        pnl = float(r.get("pnl_usd") or 0)
        held += pnl
        trig = next((m for m in marks[int(r["ts"])] if m.get("p_side") is not None and m["p_side"] < trigger and m.get("bid")), None)
        if trig is None:
            exited += pnl
            continue
        hit += 1
        shares, bid = float(r.get("filled_shares") or 0), float(trig["bid"])
        exited += shares * (bid - fee_rate * bid * (1 - bid)) - float(r.get("cost_usd") or 0)
    lines = []
    if real:
        pnl = sum(float(r.get("pnl_usd") or 0) for r in real)
        lines.append(f"Saídas antecipadas de verdade: {len(real)} | PnL US$ {pnl:+.2f}")
    if n:
        lines.append(f"Saída antecipada (contrafactual, gatilho p do lado < {trigger:.2f}, venda no bid com taxa): {n} posições marcadas, gatilho em {hit} | PnL segurando {held:+.2f} | PnL saindo {exited:+.2f}")
    return lines


FILL_TERMINAL = ("filled", "settled", "closed", "unfilled", "skipped")


def section_fill_rate(rows: List[dict]) -> List[str]:
    """Por JANELA que postou, não por ordem: recotar não é falhar."""
    def rate(kind: str):
        g = [r for r in rows if r.get("entry_kind") == kind and r.get("status") in FILL_TERMINAL]
        if not g:
            return None
        got = sum(1 for r in g if r["status"] in ("filled", "settled", "closed"))
        return got, len(g)

    mk, tk = rate("maker"), rate("taker")
    if not mk:
        return []
    line = f"Entrada por janela que postou — maker: {mk[0]}/{mk[1]} ({mk[0] / mk[1]:.0%})"
    if tk:
        line += f" | taker: {tk[0]}/{tk[1]} ({tk[0] / tk[1]:.0%})"
    line += "  (é esta taxa que decide maker × taker)"
    return [line]


def trade_stats(rows: List[dict]) -> Dict[str, Any]:
    s = [r for r in rows if r.get("status") in ("settled", "closed")]
    return {
        "settled": len(s),
        "wins": sum(1 for r in s if (r.get("pnl_usd") or 0) > 0),
        "pnl": sum(r.get("pnl_usd") or 0 for r in s) + sum(r.get("partial_pnl_usd") or 0 for r in rows),
        "staked": sum(r.get("cost_usd") or 0 for r in s),
        "ts": {int(r["ts"]) for r in s},
    }


def pnl_by_window(rows: List[dict]) -> Dict[int, float]:
    """PnL realizado por janela: liquidada, fechada antes do fim, e vendas parciais de qualquer janela."""
    out: Dict[int, float] = {}
    for r in rows:
        v = (r.get("partial_pnl_usd") or 0.0)
        if r.get("status") in ("settled", "closed"):
            v += r.get("pnl_usd") or 0.0
        elif not v:
            continue
        out[int(r["ts"])] = v
    return out


def paired(a: Dict[int, float], b: Dict[int, float]) -> Dict[str, Any]:
    """Comparação pareada: só as janelas que os DOIS motores resolveram. Motores nascidos em horas
    diferentes veem janelas diferentes, e somar tudo compara sorte, não parâmetro."""
    common = sorted(set(a) & set(b))
    diffs = [a[t] - b[t] for t in common]
    n = len(diffs)
    if n == 0:
        return {"n": 0}
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1) if n > 1 else 0.0
    se = math.sqrt(var / n) if n > 1 else 0.0
    wins = sum(1 for d in diffs if d > 1e-9)
    ties = sum(1 for d in diffs if abs(d) <= 1e-9)
    # Janelas necessárias para o efeito observado passar de 2 erros-padrão (regra de bolso, não teste formal).
    need = int(math.ceil(4 * var / (mean ** 2))) if mean and var else None
    # Variância zero com média não nula é o caso MAIS conclusivo (efeito idêntico em toda janela),
    # não o menos: o teste é média contra dois erros-padrão, com um mínimo de janelas.
    conclusive = n >= 5 and abs(mean) > 2 * se
    return {"n": n, "total": sum(diffs), "mean": mean, "se": se, "wins": wins, "ties": ties,
            "conclusive": conclusive, "need": need}


def section_compare(base_name: str, base_rows: List[dict], others: Dict[str, List[dict]]) -> List[str]:
    if not others:
        return []
    # A referência é o shadow "control" (mesmos parâmetros do live, mesmo simulador de fill). Contra o
    # live a comparação não vale: lá o fill é real.
    pnl = {n: pnl_by_window(r) for n, r in others.items()}
    ref = "control" if "control" in pnl else None
    lines = ["Comparação pareada (só janelas que os dois resolveram; referência: "
             + (f"shadow {ref}" if ref else "motor principal") + ")",
             "  motor        janelas   soma     por janela   ganhou/empatou   veredito"]
    base = pnl[ref] if ref else pnl_by_window(base_rows)
    for name, p in pnl.items():
        if name == ref:
            continue
        r = paired(p, base)
        if not r["n"]:
            lines.append(f"  {name:<12} {'—':>7}   (ainda não dividiu janela resolvida com a referência)")
            continue
        verdict = ("melhor, já fora do ruído" if r["conclusive"] and r["mean"] > 0 else
                   "pior, já fora do ruído" if r["conclusive"] else
                   f"ruído; ~{r['need']} janelas para decidir" if r["need"] and r["need"] < 100000 else "ruído")
        lines.append(f"  {name:<12} {r['n']:>7}  {r['total']:>+7.2f}   {r['mean']:>+8.3f}   {r['wins']:>3}/{r['ties']:<3}        {verdict}")
    tot = {n: (len(p), sum(p.values())) for n, p in pnl.items()}
    lines.append("  totais brutos (janelas diferentes por motor, não comparáveis entre si): "
                 + " | ".join(f"{n} {v:+.2f} em {c}" for n, (c, v) in tot.items()))
    return lines


# ------------------------------------------------------------------ relatório
def render(data_dir: Path, compare: Optional[Dict[str, Path]] = None, prior_1s: float = 5.7e-5) -> str:
    d = Path(data_dir)
    rows, events = load_rows(d / "ledger.sqlite"), load_events(d / "journal.jsonl")
    outcome = outcomes_by_ts(rows, events)
    evals = _evals_with_outcome(events, outcome, kinds=("eval",))  # amostragem regular de 20 s
    decisions = _evals_with_outcome(events, outcome, kinds=("decision",))
    blocks: List[List[str]] = [
        [f"Janelas com resultado conhecido: {len(outcome)} de {len(rows)} | avaliações pareadas: {len(evals)} | decisões com Jev: {len(decisions)}"],
        section_model_vs_market(evals),
        section_probability(evals),
        section_phase(evals),
        section_regime(decisions),
        section_veto(decisions),
        section_execution(events),
        section_taker(evals),
        section_sigma(rows, prior_1s),
        section_early_exit(rows, events),
        section_compare(d.name, rows, {n: load_rows(Path(p) / "ledger.sqlite") for n, p in (compare or {}).items()}),
        section_fill_rate(rows),
    ]
    if len(outcome) < 100:
        blocks.append([f"Amostra: {len(outcome)} janelas. Abaixo de ~100 qualquer diferença aqui é ruído; serve para acompanhar, não para mudar estratégia."])
    return "\n\n".join("\n".join(b) for b in blocks if b)
