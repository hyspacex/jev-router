# Evaluation reports

Dated runs, newest first. Each directory holds its plan or method, the
measurements with their intervals, a manifest, and the scripts that produced
them. Live scripts spend provider quota; reply text stays in the git-ignored
`evals/.cache/`. Verdicts that change the model pool are recorded in
[POOL.md](../../POOL.md), and classifier changes in
[EXPERIMENTS.md](../EXPERIMENTS.md).

| Report | Question | Outcome |
| --- | --- | --- |
| [2026-10-27 DeepSeek rerun](2026-10-27-deepseek-rerun/SUMMARY.md) | Does DeepSeek V4.1 Flash 1017 beat GLM-5.3 on easy and moderate text when reasoning, grading and load are fair? | No change: level on quality, slower because it always reasons. |
| [2026-10-27 model refresh](2026-10-27-model-refresh/SUMMARY.md) | Do DeepSeek 1017 or GPT-6.1 Sol earn a place in the pool? | Sol 6.1 low added as a moderate-text fallback. Its DeepSeek part is re-examined by the rerun. |
| [2026-09-20 GLM tool pilot](2026-09-20-glm-tool-pilot/summary.md) | Can GLM-5.3 handle tool-driven coding sessions? | Pilot pass on the bounded tasks; broad tool qualification inconclusive. |
| [2026-09-20 GLM effort](2026-09-20-glm-effort/summary.md) | Which GLM effort qualifies for moderate tool routing? | Deployed, after a shuffled-label control discrepancy was resolved. |
| [2026-09-19 session pilot](2026-09-19-session-pilot/summary.md) | Fixed model, simple rules or Jev, over eight coding tasks? | 24 of 24 sessions passed; adaptive effort was off. |

Do not read an older report as a measurement of the current configuration.
