# local_claude

Run [Claude Code](https://docs.anthropic.com/en/docs/claude-code) against local models via [LM Studio](https://lmstudio.ai) — no Anthropic API key needed.

This is a lightweight proxy that sits between Claude Code and LM Studio, handling compatibility issues that would otherwise cause hangs or errors:

Haiku interception — Claude Code sends background requests to Haiku models; these are faked instantly so they don't block your local model
Image relocation — moves images from tool_result blocks (which LM Studio rejects) into the surrounding user message
Token counting — fakes the /count_tokens endpoint
max_tokens boost — increases max_tokens for thinking models that need headroom

## Quick start

1. Install [uv](https://docs.astral.sh/uv/), [LM Studio](https://lmstudio.ai), and [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`npm i -g @anthropic-ai/claude-code`)
2. Clone and run:

```bash
git clone https://github.com/debbevi/local_claude.git
cd local_claude
./local_claude
```

That's it. The script uses `uv` to automatically manage Python and dependencies — no virtual env or `pip install` needed.

It will load your model in LM Studio, start a compatibility proxy, and launch Claude Code pointed at it.

### Options

```
--model <name>   Model to load (default: qwen3.5-122b-a10b@4bit)
--ctx <length>   Context length (default: 262144)
--stop           Stop the proxy
```

## How it works

A lightweight proxy (`proxy.py`) sits between Claude Code and LM Studio, handling compatibility issues that would otherwise cause hangs or errors — fake Haiku responses, image relocation, token counting, and max_tokens adjustments.

```
Claude Code  →  proxy (:4000)  →  LM Studio (:1234)
```

Your regular Claude Code config isn't affected — the script runs in an isolated `$HOME` (`~/.local_claude`).

## License

MIT
