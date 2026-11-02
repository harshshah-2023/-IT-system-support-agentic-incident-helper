# Measured results

From the final run on `gpt-4o-mini` (fast tier) and `gpt-4o` (reasoning tier). Model output varies, so scores move between runs: vector correctness ranged from 0.79 to 0.90 across runs, vectorless from 0.89 to 0.90 after the reasoning-tier change.

## Tests

144 passed: 140 offline and 4 live. Browser journey passed with no console errors and no mobile overflow. See `pytest.log` and `ui-e2e.log`.

## Vector vs vectorless, three multi-section questions

| Question | Method | Latency (ms) | Input tokens | Output tokens | LLM calls | Accuracy (judge) |
|---|---|---|---|---|---|---|
| Q1 | vector | 2609 | 1142 | 184 | 1 | 0.57 |
| Q1 | vectorless | 16539 | 2165 | 472 | 3 | 1.00 |
| Q2 | vector | 2088 | 1258 | 143 | 1 | 0.33 |
| Q2 | vectorless | 28380 | 8343 | 1920 | 11 | 0.33 |
| Q3 | vector | 2753 | 1337 | 141 | 1 | 1.00 |
| Q3 | vectorless | 22859 | 4323 | 883 | 6 | 0.50 |

Vector is about 6 to 13 times faster here and uses far fewer tokens. Neither wins on accuracy across all three: vectorless took Q1, vector took Q3, and both struggled with Q2, which spans three chapters. Questions are in `cli.py` as `COMPARISON_QUESTIONS`.

## Golden dataset, six questions (local `evaluate()`)

| Metric | Vector | Vectorless |
|---|---|---|
| Correctness | 0.896 | 0.903 |
| Groundedness | 1.00 | 1.00 |
| Answer relevance | 1.00 | 1.00 |

Full per-question scores and the remaining metrics are in `task6.log`.

## Hallucination test

| Method | Groundedness, normal | Groundedness, tampered |
|---|---|---|
| Vector | 1.00 | 0.61 |
| Vectorless | 1.00 | 0.61 |

## Gateway

- Cache hit about 0.06 ms for exact, about 10 ms for semantic, against a 50 ms limit (`task7.log`).
- Real 401 from a corrupted key: `fallback_triggered: true`, served by `gpt-4o-mini` (`task7-fallback-real-401.log`).
- Simulated 429: same outcome (`task7-fallback-429.log`).

Cost figures are estimates from list prices in `gateway/pricing.py`.
