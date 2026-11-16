# ReelDeck

Stdlib Python server (`server.py`) and plain ES modules in `static/`, no build
step. Each module self-checks: see "Development" in the README.

## publik API
- Base URL: https://publikhq.com/api/v1 (OpenAI Chat Completions, Responses, Anthropic Messages)
- Models: publik-fast, publik-balanced, publik-smart only; never a vendor model name
- Sign in with publik: the publik module's gate runs at start-up, before the first screen; it also sends usage telemetry
- Key: per-install `pk_` from POST /installs, stored at the publik credential path; env PUBLIK_API_KEY / PUBLIK_API_BASE_URL override
- App token: the `PUBLIK_APP_TOKEN` repository secret; the release workflow writes it to publik-app-token.txt in the image. A source checkout has none and skips the gate
- 402: show error.message + error.top_up_url only
- Show use: GET /wallet used_percent or prepaid_remaining_percent, never money
- Never print or log a key
- Docs: https://publikhq.com/llms.txt, https://publikhq.com/developers, https://publikhq.com/skills/api.md
