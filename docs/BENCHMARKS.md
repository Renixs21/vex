# Speed and token measurements (2026-10-03)

Harness: `python tools_dev/bench_turns.py --provider groq|gemini --label X [--all-tools] [--reasoning ""]`. Real LLM and real tools,
five commands: set the volume to 20 percent, set the brightness to 40, create a desktop file, run `Get-Date` in PowerShell,
remember a fact. Volume/brightness are restored afterwards. Raw results: `logs/bench_*.json`.

## Groq (`openai/gpt-oss-120b`, free tier: 8,000 tokens/minute, 1,000 requests/day)

| Run | What | Avg turn | Requests (failed 429) | Prompt tokens / request | Tools sent |
|---|---|---|---|---|---|
| before | the old code, before any change | 13.0 s | 10 (2 hidden SDK retries) | 2,406 | 37 |
| control 1 | all tools, no reasoning settings (old behaviour, new clients) | 23.4 s | 18 (8) | 2,353 | 38 |
| control 2 | same | 16.0 s | 15 (5) | 2,308 | 38 |
| after 1 | tool groups + reasoning low | 16.4 s | 10 (0) | 1,468 | 15.2 |
| after 2 | same | 9.8 s | 15 (5) | 1,429 | 14.4 |
| after 3 | same | 5.8 s | 12 (2) | 1,430 | 14.3 |

Reading it: tokens per request fall about 40% (2.3-2.4k to 1.4-1.5k) and tools sent fall from 38 to about 15. Under identical
conditions the old behaviour averaged 19.7 s per turn and 6.5 rate-limit failures per run, the new one 10.7 s and 2.3.
Wall-clock time is noisy (identical Groq requests took 0.7 to 7.5 s) so treat turn times as indicative. Five commands in under a
minute still exceed 8,000 tokens/minute on this tier, which shows up as the remaining 429s; spaced out like real use they don't.

## Gemini

`gemini-2.5-flash` before: 2,910 prompt tokens/request, three empty replies (now handled as failures), 13.3 s/turn.
`gemini-3.5-flash-lite` after: 1,767 prompt tokens/request (-39%), 10/10 requests succeeded, 14.8 s/turn, but one turn waited ~40 s
in Vex's own limiter because the free tier allows only 5 requests/minute. Different models, so only the token numbers compare.

Free-tier facts found on the way: `gemini-flash-latest` (now Gemini 3.8 Flash) and `gemini-2.5-flash` allow only 20 requests per day
per model on this project; each model has its own quota. Config now orders Groq first, Gemini second.
