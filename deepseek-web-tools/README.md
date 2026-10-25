# dsh-deepseek-web-tools

Wires the official [DeepSeek Search / Fetch API](https://api-docs.deepseek.com/zh-cn/guides/search/) into DSH's `ctx.web` seam as the `deepseek-tools` search and fetch providers, with the previously shipped backends kept as an automatic fallback chain and a one-command bundle-level rollback.

- `POST /v1/tools/search` — structured results in ~0.4 s (no model turn).
- `POST /v1/tools/fetch` — one page as Markdown in ~0.2 s (server-side extraction).

Fallback: a non-cancellation failure of the primary path is handed to the fallback provider registered in the same capability registry — search → `deepseek-official` (the Messages adapter), fetch → `http` (the anonymous local fetcher). A `401` from an account-ticket request is first downgraded to the stored API key. Cancellation never falls back. When both paths fail, one error carries both causes.

Rollback is bundle state, not profile surgery:

```text
plugin_manager({ action: "set_bundle", target: "@local/dsh-deepseek-web-tools", enabled: false })
```

Disabling the bundle removes its patch layer, which restores the base composition (`deepseek-official` + `http`) exactly.

See [README.zh.md](README.zh.md) for the full configuration table, fallback trigger matrix, session events, billing/rate-limit notes, and verification record.
