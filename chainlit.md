# Dexter

Search the web, crunch numbers, read your PDFs, check markets and weather, and
(for demos) run a Cursor Cloud Agent against a GitHub repo.

## Agent tools

`retriever_tool` `web_search` `fetch_url` `wikipedia` `extract_text_from_image`
`get_current_datetime` `get_weather` `get_stock_price` `get_stock_history`
`purchase_stock` `inspect_table` `run_pandas_code` `plot_data` `remember`
`recall` `forget` `cursor_coding_agent`

## Cursor coding demo

Requires `CURSOR_API_KEY` and usually `CURSOR_DEMO_REPO` (GitHub `org/repo` or
URL). Optional: `CURSOR_AUTO_CREATE_PR=true` to open a PR after the run.
Uses Cursor Pro usage (`composer-2.5`). Cloud agent runs often take 30–120s.

Use the settings gear next to the composer to switch models mid-conversation.
