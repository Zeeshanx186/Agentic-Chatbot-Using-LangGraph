# Agentic-Chatbot-Using-LangGraph

Streamlit chatbot built on LangGraph. Tool-calling agent with web search, weather,
stock lookup, math, and RAG over uploaded PDFs. Runs locally or in Docker.

---

## Table of contents

- [Quick start](#quick-start)
- [Environment variables](#environment-variables)
- [Running locally](#running-locally)
- [Docker](#docker)
  - [Build](#build)
  - [Run](#run)
  - [Inspect](#inspect)
  - [Stop and remove containers](#stop-and-remove-containers)
  - [Remove images](#remove-images)
  - [Cleanup and disk space](#cleanup-and-disk-space)
  - [Rebuild loop](#rebuild-loop)
- [Troubleshooting](#troubleshooting)
- [Project files](#project-files)
- [Notes and TODO](#notes-and-todo)

---

## Quick start

Image already built, just want it running:

```bash
docker run -d --name agenticbot -p 8502:8501 --env-file .env zeeshanyq/agenticbot:latest
```

Open [http://localhost:850](http://localhost:8502)1

---

## Environment variables

All secrets live in `.env` at the project root. **Not committed** — excluded by
`.gitignore` and `.dockerignore`.

| Variable                 | Required          | Used for                                              |
| ------------------------ | ----------------- | ----------------------------------------------------- |
| `GROQ_API_KEY`         | yes               | Main LLM provider                                     |
| `TAVILY_API_KEY`       | yes               | Web search tool                                       |
| `OPENWEATHER_API_KEY`  | for weather tool  | Weather lookups                                       |
| `WEATHERSTACK_API_KEY` | optional          | Alternate weather source                              |
| `ALPHAVANTAGE_API_KEY` | for stock tool    | Stock prices                                          |
| `OPENROUTER_API_KEY`   | optional          | OpenRouter models in picker                           |
| `OPENAI_API_KEY`       | optional          | OpenAI models                                         |
| `GOOGLE_API_KEY`       | for Google models | Gemini models in picker —**currently missing** |
| `LANGSMITH_TRACING`    | optional          | Trace runs to LangSmith                               |
| `LANGSMITH_ENDPOINT`   | optional          | LangSmith endpoint                                    |
| `LANGSMITH_API_KEY`    | optional          | LangSmith auth                                        |
| `LANGSMITH_PROJECT`    | optional          | LangSmith project name                                |

### Format matters

Write entries with **no spaces around `=`**:

```
GROQ_API_KEY=gsk_abc123
```

Not `GROQ_API_KEY = gsk_abc123`. Running locally, python-dotenv trims the spaces
and it works fine — but Docker's `--env-file` parser does not. It either errors
out with `contains whitespaces`, or silently passes the value with a leading
space attached, and the API rejects the key. See [Troubleshooting](#troubleshooting).

---

## Running locally

Dependencies are managed with **uv** (`pyproject.toml` + `uv.lock`).

Install:

```bash
uv sync
```

Run:

```bash
uv run streamlit run app.py
```

Opens on [http://localhost:8501](http://localhost:8501)

---

## Docker

Image name used throughout: `zeeshanyq/agenticbot:latest`

### Build

Normal build:

```bash
docker build -t zeeshanyq/agenticbot:latest .
```

Ignore all cached layers — use when a stale layer is suspected:

```bash
docker build --no-cache -t zeeshanyq/agenticbot:latest .
```

First build takes ~5-10 minutes: `sentence-transformers` pulls torch,
transformers, scikit-learn and scipy — roughly 1GB of wheels. It's bandwidth,
not CPU. Later builds are seconds as long as `requirements.txt` is untouched,
because the Dockerfile installs dependencies *before* copying the app code.

### Run

Foreground — logs print to the terminal, Ctrl+C stops it:

```bash
docker run -p 8502:8501 --env-file .env zeeshanyq/agenticbot:latest
```

Detached and named — the normal way:

```bash
docker run -d --name agenticbot -p 8502:8501 --env-file .env zeeshanyq/agenticbot:latest
```

Throwaway run, container auto-deletes on stop:

```bash
docker run --rm -p 8502:8501 --env-file .env zeeshanyq/agenticbot:latest
```

**About `-p 8502:8501`:** left side is the port on your machine, right side is
the port inside the container. Streamlit always listens on 8501 internally.
Using 8502 outside keeps the local `streamlit run` on 8501 free, so both can run
at once. `-p 8501:8501` works too if nothing local is using it.

**Env vars are read at run time, not baked into the image.** Editing `.env`
needs only a container restart, never a rebuild.

### Inspect

Running containers:

```bash
docker ps
```

All containers, stopped ones included:

```bash
docker ps -a
```

Images:

```bash
docker images
```

Follow logs of a detached container:

```bash
docker logs -f agenticbot
```

Shell inside a running container:

```bash
docker exec -it agenticbot /bin/bash
```

### Stop and remove containers

Stop:

```bash
docker stop agenticbot
```

Start the same container again, state preserved:

```bash
docker start agenticbot
```

Remove a stopped container:

```bash
docker rm agenticbot
```

Stop and remove in one step:

```bash
docker rm -f agenticbot
```

A named container can't be created twice — remove the old one before reusing
the name `agenticbot`.

### Remove images

```bash
docker rmi zeeshanyq/agenticbot:latest
```

An image won't delete while any container references it, even a stopped one.
Remove those containers first.

### Cleanup and disk space

Check what's using space before deciding anything:

```bash
docker system df
```

Remove all stopped containers:

```bash
docker container prune
```

Remove dangling `<none>` images left behind by rebuilds. Safe, and the one
that's usually wanted — every rebuild orphans the previous image:

```bash
docker image prune
```

**Destructive.** Removes stopped containers, unused networks, dangling images,
*and the whole build cache*. The next build re-downloads the full ~1GB of ML
dependencies:

```bash
docker system prune
```

**Most destructive.** Everything above, plus every image not attached to a
running container — including this app's image and the `python:3.11-slim-buster`
base. Full rebuild from zero afterward:

```bash
docker system prune -a
```

### Rebuild loop

After changing code:

```bash
docker rm -f agenticbot
```

```bash
docker build -t zeeshanyq/agenticbot:latest .
```

```bash
docker run -d --name agenticbot -p 8502:8501 --env-file .env zeeshanyq/agenticbot:latest
```

Step 2 is fast unless `requirements.txt` changed.

---

## Troubleshooting

### `Could not open requirements file: requirements.txt`

The Dockerfile ran `pip install` before copying files in. Order must be: copy
`requirements.txt`, install, *then* copy the rest of the code. Splitting it that
way is also what keeps the install layer cached across code edits.

### `invalid env file (.env): variable 'X ' contains whitespaces`

A key in `.env` has a trailing space before the `=`. Docker won't accept it.
Remove spaces around every `=`.

### `Error: Invalid OpenWeather API key` (or any key failing only in Docker)

Same root cause, quieter symptom. `KEY = value` sends the value to the container
with a **leading space** attached. python-dotenv strips it locally, so the app
works outside Docker and fails inside. Check for stray whitespace without
printing the secrets themselves:

```bash
awk -F= '/^[A-Za-z_]/ {v=substr($0,index($0,"=")+1); printf "%-24s len=%d\n", $1, length(v)}' .env
```

Compare each length against what the provider issues. An OpenWeather key is
exactly 32 characters — 33 means a space snuck in.

### `ports are not available: ... bind: Only one usage of each socket address`

Something already holds that port, usually a local `streamlit run`. Find it:

```bash
netstat -ano | grep ":8501"
```

Then either map a different host port (`-p 8502:8501`), or stop that process
with `taskkill //PID <pid> //F`.

### Image is enormous (6-7GB)

`torch` defaults to the CUDA build plus several GB of NVIDIA libraries. The
first line of `requirements.txt` pins the CPU-only wheel instead:

```
--extra-index-url https://download.pytorch.org/whl/cpu
```

Keep that line at the top. CPU is correct here — embedding a few PDFs doesn't
need a GPU, and containers have no GPU access unless explicitly configured.

---

## Project files

| File                             | Purpose                                                |
| -------------------------------- | ------------------------------------------------------ |
| `app.py`                       | Streamlit UI, chat loop, file upload                   |
| `backend.py`                   | LangGraph graph, tools, model selection                |
| `chat_store.py`                | Chat persistence, titles, model catalog                |
| `pyproject.toml` + `uv.lock` | Dependencies for local dev (uv)                        |
| `requirements.txt`             | Dependencies for the Docker build (pip)                |
| `Dockerfile`                   | Image definition                                       |
| `.dockerignore`                | Keeps secrets,`.git`, and local DBs out of the image |
| `.env`                         | API keys — never committed                            |
| `agentic_chatbot.db`           | SQLite checkpointer, local chat history                |

---

## Notes and TODO

- **Dependencies are defined twice** — `pyproject.toml` for local uv, and
  `requirements.txt` for Docker. These will drift. Planned fix: switch the
  Dockerfile to uv, use `uv.lock` for exact reproducibility, delete
  `requirements.txt`. Requires removing `uv.lock` from `.dockerignore`.
- **`pyproject.toml` still lists unused packages**: `ddgs`, `langchain-chroma`,
  `langchain-openai`, `onnxruntime`, `openpyxl`, `pandas`, `rapidocr`,
  `tabulate`, `wikipedia`, `xlrd`. Nothing imports them. Trimming these shrinks
  the local env considerably. `requirements.txt` has already been trimmed.
- **No pip download cache in the Docker build.** Adding a cache mount makes
  dependency changes rebuild in seconds instead of minutes:
  `RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt`
- **`GOOGLE_API_KEY` is missing** from `.env`, but the model picker offers a
  Google provider option. Selecting a Gemini model will fail until it's added.
- **`ALPHAVANTAGE_API_KEY` was once hardcoded in the source.** If that ever hit
  git, it's still in the history. Rotating it at AlphaVantage is the only real fix.
- Base image is `python:3.11-slim-buster` (Debian Buster, end-of-life, no
  security updates). Fine for a learning project; `python:3.11-slim-bookworm` is
  the drop-in replacement if this ever goes anywhere real.
