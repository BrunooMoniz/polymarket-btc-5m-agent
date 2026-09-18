# Jev BTC 5m Engine (Polymarket "Bitcoin Up or Down" de 5 minutos)

Agente para o mercado `btc-updown-5m-{ts}` da Polymarket. O código precifica; o Jev
(TypeSafe System One) dá bom senso e veta. Nunca opera live por omissão.

> **Aviso.** Em `EXECUTION_MODE=live` isto manda ordem de verdade e perde dinheiro de verdade. A Polymarket
> restringe a abertura de posição em várias jurisdições (docs `/developers/CLOB/geoblock`); o motor confere o
> país da saída e se recusa a operar de onde não pode, mas cumprir os termos do serviço e a lei local é
> responsabilidade de quem roda. Comece em `paper`, com `SHADOW_PROFILES`, e só depois pense em live.

## Como funciona (uma janela de 5 minutos)
1. Mercado pela slug da janela (Gamma). `clobTokenIds[0]` = Up, `[1]` = Down.
2. Price to Beat pelo endpoint `crypto-price?variant=fiveminute` (abertura Chainlink).
3. Feed Chainlink BTC/USD ao vivo (`wss://ws-live-data.polymarket.com`), a mesma série que resolve.
4. `P(Up) = Φ(d / (σ·√τ))` em código, com σ realizada por segundo do próprio feed.
5. Se houver candidato com edge maker ≥ `MIN_NET_EDGE`, uma chamada ao Jev com estado
   sem odds: regime (Score, ajusta σ), janela anômala (Noul, veta) e direção pela regra
   exata de resolução (Noul, veta se discordar do modelo). Os dois papéis são separáveis por
   `JEV_GATE` (veto) e `JEV_REGIME_ADJUST` (σ); com os dois desligados o motor nem chama o Jev.
6. Ordem maker `post_only` um tick acima do bid (taxa zero), TTL curto, até `MAX_REQUOTES`.
   Uma posição por janela, persistida em SQLite: reinício não reentra.
7. Liquidação pelo `closePrice` do crypto-price (empate = Up); PnL no ledger.
8. Faixa operacional: pula os primeiros 30 s e os últimos 25 s da janela. Stop diário e kill switch (`data/KILL`).

## Observação (sem olhar o servidor)
- **Telegram** (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`): fill, liquidação com PnL do dia, erro de ordem, recusa de
  região, egress fora/normalizado, cancelamento não confirmado, kill switch, stop diário, feed parado, carteira
  divergente, saldo preso esperando resgate, erro no loop e o resumo do dia (o dia do ledger é UTC: vira às 21:00 de
  Brasília) seguido da calibração acumulada. Fila em thread própria; falha de envio nunca afeta o motor.
- **Comparação pareada**: o relatório compara cada shadow com o `control` **só nas janelas que os dois resolveram**,
  com média por janela, quantas ele ganhou e uma estimativa de quantas janelas faltam para o efeito sair do ruído.
  Motores nascidos em horas diferentes veem janelas diferentes; somar tudo compara sorte, não parâmetro.
- **Calibração** (`report_5m.py`): Brier por faixa de probabilidade, fase da janela e regime do Jev, modelo × mid do
  book, efeito do veto, execução (fill, rejeição por book cruzado, latência do post, tempo até o fill), taker
  hipotético, σ realizada × prior e o contrafactual de saída antecipada. O motor grava o resultado de TODA janela
  vista (`outcome`), não só das apostadas, e marca a posição aberta a mercado a cada 20 s (`mark`).
- **Vigia de carteira**: a cada 5 min compara colateral on-chain + posições 5m (data-api) com o PnL do ledger;
  desvio > `WALLET_DIVERGENCE_USD` em duas leituras seguidas alerta e rebaseia. O motor **não resgata** posição
  ganha: com saldo livre zerado e ganho parado, o alerta avisa (foi o que parou o motor por 6 h em 18/09/2026).
- **Vigia de egress com failover**: a cada `EGRESS_CHECK_S` confere o país da saída do CLOB contra a lista da
  Polymarket (docs `/developers/CLOB/geoblock`). `CLOB_SOCKS_PROXY` é a rota principal e `CLOB_EGRESS_FALLBACKS`
  (lista separada por vírgula) são as reservas: rota caída, não identificada ou com 403 de região vai para
  quarentena e a próxima assume; a troca é aplicada pelo motor entre ordens, nunca no meio de uma chamada.
  Sem nenhuma rota de jurisdição aceita o motor para de postar (falha fechada; nunca posta de região bloqueada).

## Evoluções sob bandeira (desligadas no live por default)
Cada hipótese tem uma chave e roda em shadow, em paper, antes de qualquer decisão no live:
- `ALLOW_TAKER=1` + `TAKER_MIN_EDGE` (0,10) + `MAKER_FILL_RATE`: entra comendo o ask (FOK, nunca deixa ordem no
  book) quando o valor esperado do taker supera o do maker, isto é, `edge_taker > MAKER_FILL_RATE × edge_maker`.
  Comparar os edges nominais não serve: o limite maker nunca passa do ask, então o edge maker é sempre maior e o
  caminho taker ficaria inalcançável. `MAKER_FILL_RATE_AUTO` mede a taxa no próprio ledger, por JANELA que postou
  (recotar não é falhar) — medida em 18/09/2026: 91%, contra os 50% que a contagem por ordem sugeria, o que mantém
  o maker à frente (0,105 contra 0,091).
- `EARLY_EXIT_P` (0 = desligado): vende no bid quando o modelo passa a dar menos que isso ao lado comprado,
  respeitando `EARLY_EXIT_MIN_PHASE_S` e `EARLY_EXIT_MIN_PROCEEDS_USD`. Venda parcial deixa o resto liquidar
  normalmente; venda inteira fecha a janela como `closed`, e o PnL entra no dia e no stop diário.
- `SIZING_MODE=conviction` + `MIN_STAKE_USD`: aposta entre o piso e `MAX_STAKE_USD` conforme o edge. O mínimo
  de 5 shares do mercado é respeitado: aposta pequena demais sobe ao piso, e se o piso não couber no teto a
  janela fica de fora com esse motivo.
- `JEV_QUESTION_SET=meta`: troca a pergunta de direção (que a fórmula responde melhor) por uma de julgamento,
  "a estimativa do modelo é confiável nesta janela?". Com `SIZING_MODE=jev` o tamanho da aposta passa a ser
  edge × confiabilidade, e `JEV_MIN_RELIABILITY` vira o veto. Motivo, medido em 99 janelas: a direção do Jev
  ficou em Brier 0,247 contra 0,194 do modelo e não acrescenta nada além dele (correlação -0,07 com o erro do
  modelo), enquanto a anomalia mostrou correlação -0,22 com o erro mesmo descontando a confiança do modelo.
- `FAVORED_SIDE_ONLY=0`: reabre o lado cauda (a calibração de 17-18/09 sugeriu algo com n=4; é ruído até ter amostra).
- `SIGMA_PRIOR_AUTO=1`: recalcula o prior de σ da série real acumulada de hora em hora, preso a ±`SIGMA_PRIOR_MAX_DRIFT`
  do valor configurado e só com `SIGMA_PRIOR_MIN_WINDOWS` janelas.

## Execução segura
- Book do lado escolhido é relido imediatamente antes do post; rejeição `post-only cruza o book` não consome
  recotação (`MAX_CROSS_RETRIES` próprio).
- Ordem sem destino confirmado (poll levantou, cancelamento sem resposta, reinício no meio do TTL) nunca libera
  nova ordem na janela: fica `quoting`/`orphan` e `resolve_open_orders` descobre depois se executou. No arranque
  live, `cancel_all` antes do primeiro passo.
- `get_order` devolve None para qualquer ordem fora do book (medido no CLOB em 18/09/2026): o destino vem dos
  trades (`get_trades`, somando `maker_orders[].matched_amount`). Ordem ainda aberta, mesmo com fill parcial,
  nunca é gravada: o resto continua casável. Antes de recotar há uma segunda consulta aos trades
  (`REQUOTE_RECHECK_S`), porque a indexação atrasa e "sem fill" cedo demais viraria posição dupla.
- Post sem resposta do CLOB (rede/timeout, sem status 4xx): a ordem pode ter sido aceita sem devolver id, então
  o motor manda `cancel_all` e encerra a janela. Erro 4xx é recusa: conta recotação.

## Outros ativos
`ASSET=btc|eth|sol` escolhe o mercado; cada ativo tem slug, símbolo, feed Chainlink, σ prior e movimento típico
próprios em `src/assets.py` (ETH e SOL medidos em 79 janelas, 18/09/2026). Um motor opera UM ativo; vários ativos
significam vários motores no mesmo processo, cada um com seu feed e seu ledger — foi assim que ETH e SOL entraram,
como shadows em paper. Liquidez medida no mesmo dia: BTC ~17,8k, ETH ~4,2k, SOL ~4,8k, com o mesmo tick de 1 centavo
e o mesmo mínimo de 5 shares; profundidade a 2 centavos é 10 a 30 vezes menor fora do BTC.

## Shadows (A/B sem risco)
`SHADOW_PROFILES=control,alt` sobe um motor PAPER por nome (inclusive de outro ativo, com `SHADOW_<NOME>_ASSET`), no mesmo processo, com o mesmo feed e os mesmos
vereditos do Jev (compartilhados), ledger próprio em `data-shadow-<nome>` e parâmetros `SHADOW_<NOME>_<VAR>` por cima
do `.env`. Shadow é sempre paper e não recebe a chave da carteira. `control` = parâmetros do live (mede o viés do
fill simulado); compare cada hipótese com `control`, nunca com o live. `python report_5m.py data-live --vs control=data-shadow-control taker=data-shadow-taker exit=data-shadow-exit`.

## Modos
- `EXECUTION_MODE=paper` (default): fills simulados só quando o ask do book chega ao nosso limite.
- `EXECUTION_MODE=live`: exige `POLYMARKET_PRIVATE_KEY` e `POLYMARKET_PROXY_WALLET`; ordens reais.

## Rodar
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # preencha TYPESAFE_API_KEY; o resto tem default
PYTHONPATH=. pytest -q          # 100% offline
python service_runner.py        # daemon (lê .env)
python report_5m.py data        # relatório: fills, PnL, calibração ([--vs nome=DIR ...] [--telegram])
```

## Variáveis (.env)
`TYPESAFE_API_KEY`, `EXECUTION_MODE`, `DATA_DIR`, `PAPER_BANKROLL_USD`, `MAX_STAKE_USD`,
`MIN_NET_EDGE`, `DAILY_LOSS_LIMIT_USD`, `ORDER_TTL_S`, `MAX_REQUOTES`,
`MAX_JEV_CALLS_PER_WINDOW`, `JEV_ANOMALY_MAX`, `JEV_MIN_SIDE_P`, `FEED_STALE_S`,
`POLYMARKET_PRIVATE_KEY`, `POLYMARKET_PROXY_WALLET`, `POLYGON_CHAIN_ID`, `CLOB_SOCKS_PROXY`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `NOTIFY_TRADES`, `EGRESS_CHECK_S`, `WALLET_CHECK_S`,
`WALLET_DIVERGENCE_USD`, `MAX_CROSS_RETRIES`, `REQUOTE_RECHECK_S`, `MARK_INTERVAL_S`, `CLOB_EGRESS_FALLBACKS`,
`ALLOW_TAKER`, `TAKER_MIN_EDGE`, `EARLY_EXIT_P`, `EARLY_EXIT_MIN_PHASE_S`, `EARLY_EXIT_MIN_PROCEEDS_USD`,
`SIZING_MODE`, `MIN_STAKE_USD`, `SIGMA_PRIOR_AUTO`, `SIGMA_PRIOR_MIN_WINDOWS`, `SIGMA_PRIOR_MAX_DRIFT`,
`SHADOW_PROFILES`, `SHADOW_<NOME>_<VAR>`.

## Estrutura
```
service_runner.py     daemon (systemd na VPS)
report_5m.py          relatório determinístico do ledger/journal
src/config.py         Settings.from_env (paper por default)
src/polymarket_5m.py  Gamma, crypto-price, CLOB book (público)
src/chainlink_feed.py WS Chainlink + PriceBuffer (σ, cruzamentos, staleness)
src/model.py          Φ, edge maker/taker, candidato, sizing
src/jev_5m.py         estado e perguntas do Jev, parse, veto
src/execution_5m.py   PaperBroker / LiveBroker (py_clob_client_v2, post_only)
src/ledger.py         SQLite por janela + journal JSONL
src/notify.py         alertas Telegram (fila, anti-rajada)
src/egress.py         vigia da saída do CLOB (só fecha)
src/wallet_watch.py   ledger × carteira real
src/calibration.py    leitura do journal (Brier, execução, contrafactuais)
src/shadow.py         motores paper ao lado do live
src/engine_5m.py      loop da janela
tests/                offline, com fixtures reais de Gamma, book e frame do WS
```
