# Spike report: vision lane bake-off (issue #21)

THROWAWAY branch `spike/provider-fit`. 20 hand-labeled frames (`labels.json`), 154 must-strings, 2 no-text frames. Recall is exact-string, case-sensitive, whitespace-collapsed. Subscription lanes cost $0; `quota_delta` is the AgentUsageBar percent change across the lane's run (integer percent, so small runs read 0).

## Batch 1, every lane

| lane | batch | recall | hits | empty | hallucinated_strings | parse_fail | id_errors | sec_per_frame | tok_per_frame | usd_total | quota_delta |
|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-bare:sonnet | 1 | 98.7 | 152/154 | 2/2 | 0 | 0 | 0 | 7.2 | 6031 | 0.0 | {'claude.five_hour': 2} |
| gemini:gemini-3.6-flash | 1 | 98.1 | 151/154 | 1/2 | 1 | 0 | 0 | 8.4 | 2438 | 0.0438 | {'claude.five_hour': 2} |
| gemini:gemini-3.5-flash-lite | 1 | 96.8 | 149/154 | 2/2 | 0 | 0 | 0 | 2.1 | 1632 | 0.0259 | {} |
| codex:gpt-5.6-sol@low | 1 | 86.4 | 133/154 | 2/2 | 0 | 0 | 0 | 12.6 | 7553 | 0.0 | {'claude.five_hour': 1, 'openai.primary': 7, 'openai.secondary': 1} |
| codex:gpt-6-astra@low | 1 | 85.7 | 132/154 | 2/2 | 0 | 0 | 0 | 14.2 | 9582 | 0.0 | {'openai.primary': 28, 'openai.secondary': 4} |
| codex:gpt-5.5@low | 1 | 83.1 | 128/154 | 2/2 | 0 | 0 | 0 | 12.0 | 9486 | 0.0 | {'claude.five_hour': 1, 'openai.primary': 11, 'openai.secondary': 2} |
| codex:gpt-5.6-sol@medium | 1 | 82.5 | 127/154 | 2/2 | 0 | 0 | 0 | 14.1 | 8123 | 0.0 | {'claude.five_hour': 2, 'openai.primary': 12, 'openai.secondary': 2} |
| codex:gpt-5.6-terra@medium | 1 | 78.6 | 121/154 | 2/2 | 0 | 0 | 0 | 12.1 | 5207 | 0.0 | {'claude.five_hour': 2, 'openai.primary': 6, 'openai.secondary': 1} |
| codex:gpt-5.6-terra@low | 1 | 75.3 | 116/154 | 2/2 | 0 | 0 | 0 | 11.0 | 4754 | 0.0 | {'claude.five_hour': 3, 'claude.seven_day': 1, 'openai.primary': 4, 'openai.secondary': 1} |
| codex:gpt-5.6-luna@low | 1 | 70.1 | 108/154 | 2/2 | 0 | 0 | 0 | 13.5 | 7653 | 0.0 | {'claude.five_hour': 4, 'openai.primary': 2} |

## Batch size on the subscription lanes

| lane | batch | recall | hits | empty | hallucinated_strings | parse_fail | id_errors | sec_per_frame | tok_per_frame | usd_total | quota_delta |
|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-bare:sonnet | 4 | 98.7 | 152/154 | 2/2 | 0 | 0 | 0 | 5.8 | 3033 | 0.0 | {'claude.five_hour': 2} |
| claude-bare:sonnet | 8 | 99.4 | 153/154 | 2/2 | 0 | 0 | 0 | 5.2 | 2635 | 0.0 | {'openai.primary': 2} |

## Token model per lane (measurement 6)

Fit `tokens_per_call = a + b * frames` from batch 1 and batch 4, then predict batch 8 and compare. Per-video estimate: `calls * a + frames * b`, with `calls = ceil(frames / batch)`. Transcript context is not in this spike's prompt; the pipeline adds about 200 tokens per transcript minute in the window, so `c` is set to 200 and calibrated by `cost.json`.

| lane | a (per call) | b (per frame) | batch-8 predicted | batch-8 measured | error |
|---|---|---|---|---|---|
| claude-bare:sonnet | 3997 | 2034 | 20267 | 21080 | 4% |

## Local STT (measurement 5)

10-minute mono 16 kHz wav cut from the kickoff walkthrough (decision 45), large-v3-turbo, this Mac, while the vision lanes and a delegate were also running:

- whisperx cpu int8: 178 s wall clock, 3.4x realtime
- mlx-whisper large-v3-turbo: 64 s wall clock, 9.4x realtime
- WhisperX has no MPS path (CTranslate2 runs CPU or CUDA), so the Apple-silicon candidate is mlx-whisper.

## Lock rule applied

See `docs/decisions.md` on main for the accepted row; the verdict text is written there, not here.
