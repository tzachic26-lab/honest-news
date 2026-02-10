# Honest News MCP Server

An MCP (Model Context Protocol) server that fetches and summarizes Israeli news
headlines, with optional political-orientation filtering and LLM-backed
summaries. Includes a small demo UI.

## Features

- Fetches Google News RSS headlines for Israel (Hebrew).
- De-duplicates similar headlines.
- Optional LLM classification for political orientation (left/right/neutral).
- MCP tools: `latest_headlines`, `summarize_news_topic`, `headline_details`.

## Requirements

- Python 3.12+
- Node.js 18+ (for the `ui` folder)

## Setup (Python)

Install dependencies with `uv` (recommended):

```bash
uv sync
```

## Environment Variables

Create a `.env` file (not committed) with:

```
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4o-mini   # optional
SYSTEM_PROMPT=...          # optional
NEWS_HEADLINES_USE_LLM=0   # set to 1 to rewrite summaries
NEWS_SEARCH_CONTEXT_LIMIT= # optional int for topic summaries
```

## Run the MCP Server

```bash
python servers/HonestNewsMCPServer.py
```

## Run the Multiplex Client

This starts the MCP server and connects it with a math server.

```bash
python main.py
```

To run a quick demo client:

```bash
python servers/HonestNewsMCPServer.py --client
```

## UI (Optional)

```bash
cd ui
npm install
npm run dev
```

To run the local UI server:

```bash
npm run server
```

## Notes

- The `.env` file is ignored in `.gitignore` to avoid committing secrets.
- If `OPENAI_API_KEY` is missing, the server falls back to RSS-only data.
