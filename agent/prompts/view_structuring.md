# Market View Structuring

You receive a free-text market view written by the agent harness on a given
market date. Convert it into structured JSON using schema version `v1`.

Output schema (all fields required unless marked optional):

```json
{
  "stance": "bullish | bearish | neutral",
  "thesis": "one-sentence core argument",
  "risks": ["risk statement", ...],
  "catalysts": ["event or date", ...],
  "confidence": 0.0-1.0,
  "related_tickers": ["TICKER", ...],
  "time_horizon_days": 0-365
}
```

Rules:
- Extract only what the view supports. Do not add market knowledge.
- `related_tickers` uses Yahoo-style symbols where identifiable.
- Preserve hedging: a tentative view gets confidence < 0.5.
- If the view is about the market as a whole, `related_tickers` may be empty.
