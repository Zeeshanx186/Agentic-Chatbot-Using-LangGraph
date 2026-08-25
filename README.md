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
- [CI/CD](#cicd)
- [Troubleshooting](#troubleshooting)
- [Project files](#project-files)
- [Notes and TODO](#notes-and-todo)

---

## Quick start

Image already built, just want it running:

```bash
docker run -d --name agenticbot -p 8502:8501 --env-file .env zeeshanyq/agenticbot:latest
```

Open <http://localhost:8502>

---

## Environment variables

All secrets live in `.env` at the project root. **Not committed** — excluded by
`.gitignore` and `.dockerignore`.

| Variable | Required | Used for |
|---|---|---|
| `GROQ_API_KEY` | yes | Main LLM provider |
| `TAVILY_API_KEY` | yes | Web search tool |
| `OPENWEATHER_API_KEY` | for weather tool | Weather lookups |
| `WEATHERSTACK_API_KEY` | optional | Alternate weather source |
| `ALPHAVANTAGE_API_KEY` | for stock tool | Stock prices |
| `OPENROUTER_API_KEY` | optional | OpenRouter models in picker |
| `OPENAI_API_KEY` | optional | OpenAI models |
| `GOOGLE_API_KEY` | for Google models | Gemini models in picker — **currently missing** |
| `LANGSMITH_TRACING` | optional | Trace runs to LangSmith |
| `LANGSMITH_ENDPOINT` | optional | LangSmith endpoint |
| `LANGSMITH_API_KEY` | optional | LangSmith auth |
| `LANGSMITH_PROJECT` | optional | LangSmith project name |

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

Opens on <http://localhost:8501>

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

## CI/CD

Workflow: [`.github/workflows/cicd.yaml`](.github/workflows/cicd.yaml)

Three jobs, run in order. Triggered by pushes to `main`, pull requests to
`main`, and manually from the Actions tab.

| Job | Runs on | Does |
|---|---|---|
| `lint` | GitHub runner | flake8. Fails only on syntax errors and undefined names; style issues are advisory |
| `push_to_registry` | GitHub runner | Builds the image. **Pushes only on `main`** — PRs build to verify the Dockerfile, never publish |
| `continuous_deployment` | **self-hosted runner on EC2** | Pulls the image, replaces the `stapp` container, verifies it is running and healthy |

The deploy job runs **on the EC2 box itself**. There is no SSH step and no
private key stored in GitHub — the runner polls GitHub for work, so every
`docker` command executes locally on that machine.

---

### AWS setup, step by step

Do these in order. Steps 1-3 are the machine, step 4 connects it to GitHub.

#### 1. Launch the EC2 instance

| Setting | Value | Why |
|---|---|---|
| AMI | Ubuntu Server 24.04 LTS | |
| Instance type | **t3.medium** minimum | The image is ~2GB and importing torch needs real RAM. t2.micro will OOM or thrash |
| Storage | **30 GB** gp3 | Default 8GB cannot hold the image plus a rollback copy plus the runner |
| Key pair | Create one, download the `.pem` | Needed for your own SSH access, not for the pipeline |

t3.medium is not free tier. If cost matters, stop the instance when you are not
using it — you pay for the EBS volume either way, but not the compute.

#### 2. Security group

Inbound rules:

| Type | Port | Source |
|---|---|---|
| SSH | 22 | **My IP** |
| Custom TCP | 8501 | My IP, or `0.0.0.0/0` if you want to share the demo |

Leave outbound at default (all traffic) — the runner needs to reach GitHub and
Docker Hub.

Never open 22 to `0.0.0.0/0`.

#### 3. Install Docker on the instance

SSH in with your `.pem`, then:

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl
```

```bash
curl -fsSL https://get.docker.com | sudo sh
```

```bash
sudo usermod -aG docker $USER && newgrp docker
```

Verify — this must succeed **without `sudo`**, or the runner will not be able to
drive Docker:

```bash
docker run --rm hello-world
```

#### 4. Install the GitHub Actions self-hosted runner

In your repo: **Settings → Actions → Runners → New self-hosted runner**, pick
**Linux / x64**. GitHub shows a set of commands containing a one-time token —
use the ones from that page, not copies from anywhere else, because the token
is short-lived and repo-specific.

They look like this:

```bash
mkdir actions-runner && cd actions-runner
```

```bash
curl -o actions-runner-linux-x64.tar.gz -L https://github.com/actions/runner/releases/download/v2.xxx.x/actions-runner-linux-x64-2.xxx.x.tar.gz
```

```bash
tar xzf ./actions-runner-linux-x64.tar.gz
```

```bash
./config.sh --url https://github.com/Zeeshanx186/Agentic-Chatbot-Using-LangGraph --token <TOKEN_FROM_GITHUB>
```

At the prompts: accept the default runner group, accept the default name, and
**leave labels empty** — the default label is `self-hosted`, which is exactly
what the workflow's `runs-on: self-hosted` matches. Adding a custom label here
without changing the workflow is the usual reason a deploy job hangs forever in
"Waiting for a runner".

Then install it as a service, so it survives logout and reboots:

```bash
sudo ./svc.sh install
```

```bash
sudo ./svc.sh start
```

Check it:

```bash
sudo ./svc.sh status
```

The runner should now show as **Idle** under Settings → Actions → Runners.

`./run.sh` also works but dies the moment your SSH session closes. Use the
service.

---

### Required GitHub secrets

**Settings → Secrets and variables → Actions → New repository secret.**

Docker Hub:

| Secret | Value |
|---|---|
| `REGISTRY` | `docker.io`. Optional — the workflow falls back to `docker.io` if unset |
| `DOCKER_USERNAME` | `zeeshanyq` |
| `DOCKER_PASSWORD` | A Docker Hub **access token** — hub.docker.com → Account Settings → Personal access tokens. The name says password; use a token. Tokens are scoped and revocable, your account password is neither |
| `IMAGE_NAME` | `agenticbot` — just the repository name, no username and no tag. The workflow builds the full path as `REGISTRY/DOCKER_USERNAME/IMAGE_NAME` |

Whatever you set `IMAGE_NAME` to becomes the Docker Hub repository name. Your
local image is `zeeshanyq/agenticbot`, so `agenticbot` keeps the two in step. If
you set something else, the pipeline still works — it just publishes to a
different repository than the one you build locally.

### Secrets this project does NOT need

Some setup guides list AWS credentials as repository secrets:

- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION`

**Do not create these.** They are needed only when the pipeline calls the AWS
API — pushing to ECR, or provisioning infrastructure. This pipeline does
neither: the image goes to Docker Hub, and the deploy runs on a self-hosted
runner that is already inside your EC2 instance, so nothing ever authenticates
to AWS from GitHub.

An `AWS_SECRET_ACCESS_KEY` is a long-lived credential to your whole AWS account.
Storing one that nothing reads is pure downside — it can leak but can never be
useful. Skip them.

Other secrets:

Application keys — same values as your local `.env`, one secret each:

| Secret | Required |
|---|---|
| `GROQ_API_KEY` | yes, deploy fails without it |
| `TAVILY_API_KEY` | yes |
| `OPENWEATHER_API_KEY` | yes |
| `ALPHAVANTAGE_API_KEY` | yes |
| `WEATHERSTACK_API_KEY` | optional |
| `OPENROUTER_API_KEY` | optional |
| `OPENAI_API_KEY` | optional |
| `GOOGLE_API_KEY` | optional, needed for Gemini models |
| `LANGSMITH_TRACING` | optional |
| `LANGSMITH_ENDPOINT` | optional |
| `LANGSMITH_API_KEY` | optional |
| `LANGSMITH_PROJECT` | optional |

Paste **only the value** into the secret box — no quotes, no `KEY=` prefix, no
trailing spaces. GitHub stores exactly what you paste.

No `.env` file is needed on the server. The deploy job passes each secret to
`docker run` with a bare `-e VAR` flag, which forwards the value from the step's
environment — so no secret value is ever written into a command line or shown in
the run log.

The four keys marked required are checked before the running container is
touched. The optional ones are forwarded whether set or not; an unset secret
arrives as an empty string, which the app treats as absent.

---

### After deploying

App URL: `http://<EC2_PUBLIC_IP>:8501`

Each push to `main` publishes three tags:

- `zeeshanyq/agenticbot:main` — **the tag the deploy job pulls**, from the branch name
- `zeeshanyq/agenticbot:sha-abc1234` — immutable, pinned to that commit
- `zeeshanyq/agenticbot:latest` — convenience tag for local use

The deploy pulls `:main`, not `:latest`. If you change the branch name or the
tagging rules, that pull breaks — they have to stay in step.

### Rollback

There is no automatic rollback. If a deploy goes bad, run a known-good sha tag
on the box by hand:

```bash
docker rm -f stapp
```

```bash
docker run -d --restart unless-stopped --name stapp -p 8501:8501 --env-file ~/.env zeeshanyq/agenticbot:sha-abc1234
```

That needs an `.env` on the server, since you are outside the pipeline that
supplies the secrets. Image pruning keeps anything newer than 24h, so recent
sha tags are still on disk.

### Container commands on the server

The deployed container is named **`stapp`**, not `agenticbot` — the local
examples earlier in this file use `agenticbot`, the pipeline uses `stapp`.

```bash
docker ps --filter "name=^stapp$"
```

```bash
docker logs -f stapp
```

### Checking the runner

On the EC2 box:

```bash
sudo ~/actions-runner/svc.sh status
```

Runner logs, when a job never starts:

```bash
sudo journalctl -u "actions.runner.*" -n 50 --no-pager
```

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

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI, chat loop, file upload |
| `backend.py` | LangGraph graph, tools, model selection |
| `chat_store.py` | Chat persistence, titles, model catalog |
| `pyproject.toml` + `uv.lock` | Dependencies for local dev (uv) |
| `requirements.txt` | Dependencies for the Docker build (pip) |
| `Dockerfile` | Image definition |
| `.dockerignore` | Keeps secrets, `.git`, and local DBs out of the image |
| `.env` | API keys — never committed |
| `agentic_chatbot.db` | SQLite checkpointer, local chat history |

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
