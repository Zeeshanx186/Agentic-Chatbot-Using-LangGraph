# CI/CD from scratch — a step-by-step guide

For a version of me who has forgotten all of this, or is starting a brand new project
and wants the same deployment pipeline on it.

Read top to bottom. Every step ends with a **Checkpoint** — something you can see or run
that proves the step worked. Do not move on until the checkpoint passes. Most of the pain
in this pipeline comes from finding out at step 11 that step 4 was wrong.

Time: about 2 hours the first time, most of it waiting for downloads.

---

## Table of contents

- [Part 0: What this project is](#part-0-what-this-project-is)
- [Part 1: What we are building and why](#part-1-what-we-are-building-and-why)
- [Part 2: Accounts and tools you need first](#part-2-accounts-and-tools-you-need-first)
- [Part 3: Get the app running on your own machine](#part-3-get-the-app-running-on-your-own-machine)
- [Part 4: Put the app in a container](#part-4-put-the-app-in-a-container)
- [Part 5: Get the code onto GitHub](#part-5-get-the-code-onto-github)
- [Part 6: Set up Docker Hub](#part-6-set-up-docker-hub)
- [Part 7: Write the workflow file](#part-7-write-the-workflow-file)
- [Part 8: Launch the EC2 server](#part-8-launch-the-ec2-server)
- [Part 9: Install Docker on the server](#part-9-install-docker-on-the-server)
- [Part 10: Turn the server into a self-hosted runner](#part-10-turn-the-server-into-a-self-hosted-runner)
- [Part 11: Add the secrets](#part-11-add-the-secrets)
- [Part 12: The first deploy](#part-12-the-first-deploy)
- [Part 13: The daily loop](#part-13-the-daily-loop)
- [Part 14: When it breaks](#part-14-when-it-breaks)
- [Part 15: Shutting it down](#part-15-shutting-it-down)
- [Appendix A: Every file, ready to copy](#appendix-a-every-file-ready-to-copy)
- [Appendix B: Glossary](#appendix-b-glossary)
- [Appendix C: Doing this on a brand new project](#appendix-c-doing-this-on-a-brand-new-project)

---

## Part 0: What this project is

Dexter, a **Chainlit chatbot**. You type a question and a team of LangGraph agents
answers it, using real tools instead of guessing.

| File | What it holds |
|---|---|
| `dexter/chainlit_app.py` | Chainlit UI — chat, file upload, Approve/Decline, streaming |
| `dexter/backend.py` | LangGraph graph, specialist agents, tools, model selection |
| `dexter/chat_store.py` | Chat titles, feedback, model catalog |
| `dexter/google_tools.py` | Gmail and Google Calendar tools |
| `dexter/cloud_store.py` | Postgres persistence when `DATABASE_URL` is set |
| `dexter/vercel_app.py` | Vercel ASGI entry |
| `deploy/Dockerfile` | Image definition. Build context is the repo root |

**How the brain works.** A message goes to a *supervisor*, which writes a plan — a list of
steps. It sends each step to whichever specialist agent owns the right tool, waits for the
result, then moves to the next step. If a step fails it replans. When the plan is done a
*synthesizer* writes the final answer out of everything collected.

The specialist agents and what they own:

| Agent | Tools |
|---|---|
| `Data_Agent` | `run_pandas_code`, `inspect_table`, `plot_data` — spreadsheets, maths, charts |
| `Research_Agent` | `web_search`, `fetch_url`, `wikipedia`, `retriever_tool` (searches your uploaded PDFs), `extract_text_from_image` (OCR) |
| `Financial_Agent` | `get_stock_price`, `get_stock_history`, `purchase_stock`, `get_weather` |
| `Memory_Agent` | `remember`, `recall`, `forget`, `get_current_datetime` |
| `Coding_Agent` | `cursor_coding_agent` |
| `Google_Agent` | Gmail and Calendar tools in `dexter/google_tools.py` |

`purchase_stock`, `gmail_send_email`, and `calendar_delete_event` **stop the graph and wait** —
that is the Approve / Decline prompt in the UI. That is a human-in-the-loop interrupt.

Chat history lives in `agentic_chatbot.db`, a SQLite file. LangGraph calls this a
checkpointer: every step of every conversation is saved, so a chat can be reopened, and an
interrupted graph can be resumed later.

**You do not need to understand any of that to do the deployment.** The pipeline below
works the same for any app that listens on a port. It is here so that when a deploy breaks
you know what you are looking at.

---

## Part 1: What we are building and why

Right now, shipping a change means: build the image, push it somewhere, SSH into a server,
pull, stop the old container, start the new one. Six manual steps, every time, and any one
of them can be forgotten at 1am.

The goal: **`git push` and the change is live.** Nothing else.

Here is the whole machine:

```
  you: git push
        |
        v
  GitHub Actions starts
        |
        +--> [job 1: lint]           runs on GitHub's computer
        |      flake8, catch syntax errors before spending 10 min on a build
        |
        +--> [job 2: build & push]   runs on GitHub's computer
        |      docker build  -->  push to Docker Hub  (tags: main, sha-abc123, latest)
        |
        +--> [job 3: deploy]         runs ON YOUR EC2 SERVER
               docker pull the new image
               docker stop + rm the old container
               docker run the new one
               poll the health endpoint until it answers
```

Each job only starts if the one before it passed. A broken build never reaches the server.

### The one idea that makes this easy: the self-hosted runner

The obvious way to run job 3 would be for GitHub to SSH into your server. That means
putting an SSH private key into GitHub, opening port 22 to GitHub's whole IP range, and
debugging SSH in a place where you cannot see the terminal.

We do the opposite. We install a small program **on the EC2 server** called a *runner*.
It phones GitHub every few seconds: "any work for me?" When job 3 comes up, the runner
picks it up and runs those `docker` commands **locally, on the server**.

Consequences worth understanding, because they explain most of Part 14:

- No SSH key in GitHub. No inbound port for the pipeline at all.
- The connection is outbound-only, from the server to GitHub.
- `docker pull` in job 3 is running on the server, as the `ubuntu` user, in whatever state
  that machine is actually in.
- **If the runner program is not running, jobs do not fail — they queue, forever.** This is
  the single most confusing failure in the whole system, and Part 10 is mostly about
  avoiding it.

### Why Docker Hub in the middle

Job 2 and job 3 are on different computers. They need somewhere to hand the image over.
Docker Hub is that place — a free public registry. The image goes up from GitHub's runner
and comes down onto your EC2 box.

The alternative is AWS ECR, which is what most tutorials use. It works, but it forces you
to put AWS access keys into GitHub so the build machine can log in. With Docker Hub and a
self-hosted runner, **no AWS credential ever leaves your laptop.** That is why this repo
has no `AWS_ACCESS_KEY_ID` secret, even though tutorials tell you to add one.

---

## Part 2: Accounts and tools you need first

Get all five of these before you start. Hunting for an account halfway through a deploy is
how mistakes happen.

| Thing | Cost | Notes |
|---|---|---|
| GitHub account | free | The repo must be one you own — you need to reach its Settings page |
| Docker Hub account | free | Free tier allows unlimited public repositories |
| AWS account | free tier / credits | Needs a card on file even if you never get billed |
| Docker Desktop on your machine | free | For building and testing locally before any of this |
| Git on your machine | free | |

Also collect the API keys the app itself needs. For this project:

| Key | Where | Free? |
|---|---|---|
| `GROQ_API_KEY` | console.groq.com | yes, generous free tier |
| `TAVILY_API_KEY` | tavily.com | yes, limited monthly searches |
| `OPENWEATHER_API_KEY` | openweathermap.org | yes |
| `ALPHAVANTAGE_API_KEY` | alphavantage.co | yes, 25 calls/day |

**Checkpoint:** run these two and get version numbers, not "command not found".

```bash
docker --version
```

```bash
git --version
```

---

## Part 3: Get the app running on your own machine

Never containerize an app you have not seen work. If it is broken locally, Docker just
gives you a slower way to discover that.

### 3.1 Install the dependencies

This project uses **uv**, a fast Python package manager. It reads `pyproject.toml` and
`uv.lock` and builds a `.venv` folder.

```bash
uv sync
```

### 3.2 Create your `.env`

Make a file called `.env` in the project root:

```
GROQ_API_KEY=
TAVILY_API_KEY=
OPENWEATHER_API_KEY=
ALPHAVANTAGE_API_KEY=
```

**No spaces around the `=`.** Write `KEY=value`, never `KEY = value`. Locally it makes no
difference — the Python library that reads this file trims the spaces for you. Inside
Docker it either crashes the container or, much worse, silently passes the value with a
leading space attached so the API rejects your key and you spend an hour thinking the key
is wrong. This will bite you in Part 4 if you ignore it.

**`.env` must never be committed.** Check that `.gitignore` contains a line saying `.env`
before your first commit. A key in git history is in git history forever; the only real
fix is to go to the provider and rotate the key.

### 3.3 Run it

```bash
uv run chainlit run dexter/chainlit_app.py
```

**Use `uv run`.** A bare `chainlit` or `python` on your PATH may be a different install
than the project's `.venv`, and imports will fail even though the lockfile is fine.

**Checkpoint:** http://localhost:8000 shows the chat screen. `GROQ_API_KEY` has to be set
before the process will even import. Ask "what is the weather in Lahore" only after the
weather key is set too. A missing tool key looks the same inside a container, only harder
to see.

---

## Part 4: Put the app in a container

A container is your app plus the exact Python version, the exact packages, and the exact
system libraries it needs, frozen into one file that runs the same on your laptop and on a
server in Stockholm.

### 4.1 Write `requirements.txt`

Docker does not use uv here — it uses plain pip. So the dependencies get listed a second
time, in `requirements.txt`.

> Yes, this means the dependency list exists twice and the two can drift apart. That is a
> real flaw. The fix is to install uv in the Dockerfile and use `uv.lock` directly, which
> is worth doing later. For now, when you add a package, remember to add it in both places.

The first line matters more than anything else in the file:

```
--extra-index-url https://download.pytorch.org/whl/cpu
```

Without it, `torch` installs its CUDA build and drags in several gigabytes of NVIDIA GPU
libraries that a container with no GPU can never use. Image goes from ~2 GB to ~7 GB.
Keep that line at the top.

### 4.2 The Dockerfile

The live file is [../deploy/Dockerfile](../deploy/Dockerfile). Build context is the
repository root, not the `deploy/` directory:

```bash
docker build -f deploy/Dockerfile -t zeeshanyq/agenticbot:latest .
```

Parts that are not obvious:

- **`PYTHONUNBUFFERED=1`** — without it, Python holds log output in a buffer and
  `docker logs` shows you nothing while the container is dying.
- **`libgomp1`** — the OpenMP runtime. `faiss-cpu` fails at import without it, and the
  error message does not mention OpenMP.
- **`COPY deploy/requirements.txt` before `COPY dexter/`** — Docker caches each step.
  Editing application code does not invalidate the install layer. First build is several
  minutes. Later builds are seconds unless the requirements file changed.
- **`google_tools.py` is inside `dexter/`** — the image used to copy a file list and
  omitted it, so the container crashed on import. Copy the package directory.
- **`--host 0.0.0.0`** — binds all interfaces. Leave it out and the container looks
  healthy in `docker ps` while the published port reaches nothing.
- **`--headless`** — Chainlit does not try to open a browser or prompt on first run.
- The process runs as a non-root user named `dexter`.

### 4.3 Write `.dockerignore`

Same idea as `.gitignore`, for the image build. Without it, `COPY . .` copies your `.env`
straight into an image you are about to publish publicly.

```
.git
.gitignore
__pycache__/
*.pyc
.venv/
venv/
.env
*.db
*.db-shm
*.db-wal
chat_titles.json
uv.lock
```

`.env` and `*.db` are the important lines. **Your secrets must not go in the image.** They
get injected at run time instead, in Part 11.

### 4.4 Build it

```bash
docker build -f deploy/Dockerfile -t zeeshanyq/agenticbot:latest .
```

The `-t` name is `yourdockerhubusername/imagename:tag`. It has to start with your Docker
Hub username or you will not be able to push it later.

First run takes 5–10 minutes and is almost entirely download.

### 4.5 Run it

```bash
docker run -d --name agenticbot -p 8001:8000 --env-file .env zeeshanyq/agenticbot:latest
```

- `-d` — detached, runs in the background
- `--name agenticbot` — so you can refer to it later. A name cannot be used twice; remove
  the old container before reusing it
- `-p 8001:8000` — **left is the port on your machine, right is the port inside the
  container.** Chainlit listens on 8000 inside. Using 8001 outside keeps a local
  `uv run chainlit` on 8000 free so both can run at once
- `--env-file .env` — passes the keys in at run time

**Env vars are read at run time, not baked into the image.** Changing `.env` needs a
container restart, never a rebuild.

**Checkpoint:** http://localhost:8001 works, and a tool call succeeds. If the page loads
but every tool errors, your `.env` has a spacing problem — go back to 3.2. This command
shows the length of each value without printing the secrets:

```bash
awk -F= '/^[A-Za-z_]/ {v=substr($0,index($0,"=")+1); printf "%-24s len=%d\n", $1, length(v)}' .env
```

An OpenWeather key is exactly 32 characters. 33 means a space snuck in.

Useful while debugging:

```bash
docker logs -f agenticbot
```

```bash
docker exec -it agenticbot /bin/bash
```

---

## Part 5: Get the code onto GitHub

Create an empty repository on GitHub — no README, no `.gitignore`, nothing, or the first
push will conflict.

```bash
git init
```

```bash
git remote add origin https://github.com/YOURNAME/YOURREPO.git
```

**Before the first commit,** confirm the secrets are excluded:

```bash
git status --short
```

If `.env` appears in that list, stop and fix `.gitignore` first. Do not commit and clean up
after — anything committed once stays in the history.

```bash
git add .
```

```bash
git commit -m "Initial commit"
```

```bash
git branch -M main
```

```bash
git push -u origin main
```

**Checkpoint:** the files are on GitHub, and `.env` is **not** among them. Click through
the file list and confirm with your eyes.

---

## Part 6: Set up Docker Hub

### 6.1 Create the repository

Docker Hub → Repositories → Create repository. Name it `agenticbot`. Public is fine and
free. Remember this name — it becomes the `IMAGE_NAME` secret.

### 6.2 Create an access token

Account Settings → Personal access tokens → Generate new token.

- Description: `github-actions`
- Permissions: **Read & Write**

Copy the token now. It is shown once.

**Use a token, not your account password.** A token can be revoked on its own without
changing your password, and it does not carry your full account privileges.

### 6.3 Prove the whole path works by hand once

Before automating something, do it manually once so you know the destination exists:

```bash
docker login -u zeeshanyq
```

```bash
docker push zeeshanyq/agenticbot:latest
```

**Checkpoint:** the tag appears on your Docker Hub repository page. If this manual push
fails, automating it will fail in exactly the same way, only with less visible output.

---

## Part 7: Write the workflow file

The path matters exactly: **`.github/workflows/cicd.yaml`**. GitHub only looks in that
folder. A typo there means nothing runs and there is no error message anywhere.

The full file is in [Appendix A](#appendix-a-every-file-ready-to-copy). Here is what each
piece is doing.

### 7.1 When it runs

```yaml
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:
```

Push to `main` builds, publishes, and deploys. A pull request builds only — it proves the
Dockerfile still works, but never logs in, never pushes, never deploys. `workflow_dispatch`
adds a "Run workflow" button in the Actions tab for when you want to redeploy without a
code change.

### 7.2 Don't let two deploys collide

```yaml
concurrency:
  group: cicd-${{ github.ref }}
  cancel-in-progress: false
```

Your server is one machine with one container name and one port. Push twice quickly and two
deploys would fight over both. This queues them. `cancel-in-progress: false` is deliberate:
never cancel a deploy that is halfway through replacing the running container, or you are
left with no container at all.

### 7.3 Job 1 — lint

flake8, in two passes:

```bash
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics --exclude "$EXCLUDES"
```

```bash
flake8 . --count --exit-zero --max-complexity=10 --max-line-length=127 --statistics --exclude "$EXCLUDES"
```

The first pass **fails the build**, and only on things that are genuinely broken — syntax
errors and undefined names. The second is `--exit-zero`, so it prints style complaints and
never blocks you.

The `--exclude` is not optional. Without it flake8 walks into any virtualenv it finds and
can die inside a dependency's source — `sympy` triggers a `RecursionError` in pyflakes.

Why lint first: it takes 20 seconds, and it stops a typo from costing you a ten-minute
Docker build.

### 7.4 Job 2 — build and push

```yaml
- name: Log in to Docker Hub
  if: github.event_name != 'pull_request'
```

Pull requests skip the login and skip the push. Only `main` publishes.

The tags come from `docker/metadata-action`:

```yaml
tags: |
  type=ref,event=branch
  type=sha,format=short
  type=raw,value=latest,enable={{is_default_branch}}
```

| Tag produced | What it is for |
|---|---|
| `:main` | **What the deploy pulls.** Comes from the branch name |
| `:sha-abc1234` | Immutable. This is your rollback — it always points at exactly one commit |
| `:latest` | Local convenience only. Nothing in the pipeline reads it |

**The deploy pulls `:main`, not `:latest`.** Rename your branch and the pull breaks.

The build cache:

```yaml
cache-from: type=registry,ref=.../IMAGE:buildcache
cache-to:   type=registry,ref=.../IMAGE:buildcache,mode=max
```

GitHub gives you a clean machine every run, so without this every single build
re-downloads the ~1 GB torch layer. This stores the layer cache in your Docker Hub repo.
Pull requests read the cache but cannot write to it, because they never logged in.

### 7.5 Job 3 — deploy

```yaml
runs-on: self-hosted
if: github.event_name != 'pull_request' && github.ref == 'refs/heads/main'
```

`runs-on: self-hosted` is what sends this job to your EC2 box instead of GitHub's.

Its steps, and why each exists:

1. **Log in to Docker Hub** — so the box can pull.
2. **Validate application secrets** — check every required key is non-empty and `exit 1` if
   not. This runs *before* anything is stopped. Skip it and a missing key gives you a
   container that starts clean, reports healthy, and fails on every single tool call at
   request time — which looks like an application bug, not a deployment one.
3. **Pull** `IMAGE:main`.
4. **Stop and remove** the old container. Both end in `|| echo "..."` so the very first
   deploy, when no container exists yet, does not fail.
5. **Run** the new container.
6. **Verify it is running** — `docker inspect`, and print the last 100 log lines if not.
7. **Verify the health endpoint** — see 7.6.
8. **Prune old images**, with `if: always()`.

Step 8 matters more than it looks. Each tag is ~2 GB, EC2 root volumes are small, and a
full disk breaks everything in a way that is very hard to read. `if: always()` means it
runs even when the deploy failed, so a red pipeline cannot fill the disk.

### 7.6 How the secrets reach the container

```bash
docker run -d \
  --restart unless-stopped \
  --name dexter \
  -p 8000:8000 \
  -e GROQ_API_KEY \
  -e TAVILY_API_KEY \
  ...
  "${IMAGE_PATH}:main"
```

`-e GROQ_API_KEY` **with no `=` and no value** tells Docker to take the value from the
step's own environment. The secret never appears in the command line and never appears in
the run log. Writing `-e GROQ_API_KEY=${{ secrets.GROQ_API_KEY }}` would work too, but puts
the value somewhere it can leak.

`--restart unless-stopped` means the container comes back by itself if the server reboots.

### 7.7 Why the health check is not optional

```bash
for attempt in $(seq 1 12); do
  if docker exec dexter python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8000/', timeout=3)" \
    >/dev/null 2>&1; then
    echo "Chainlit application is healthy."
    exit 0
  fi
  echo "Waiting for Chainlit to become ready..."
  sleep 5
done
```

This image imports torch and sentence-transformers at startup, which takes several seconds.
"The process exists" is not the same as "the app answers requests", and a configuration
crash can land later still. Without this poll, the pipeline goes green while the app is
broken, and you find out from a user instead of from CI.

Twelve attempts, five seconds apart — one minute of grace, then fail and dump the logs.

---

## Part 8: Launch the EC2 server

EC2 is a rented Linux computer. Console → EC2 → Launch instance.

| Setting | Choose | Why |
|---|---|---|
| Name | `Agentic-chatbot-Machine` | anything |
| AMI | Ubuntu (latest LTS) | the runner install commands below assume Ubuntu |
| Instance type | **t3.micro** | ~$0.012/hr. See the cost note below |
| Key pair | create one, download the `.pem` | your only way to SSH in. Lose it and the instance is unreachable |
| Storage | 30–32 GiB gp3 | the default 8 GiB fills up fast at ~2 GB per image tag |

Security group — inbound rules:

| Type | Port | Source | Why |
|---|---|---|---|
| SSH | 22 | **My IP** | you, setting the box up |
| Custom TCP | 8000 | 0.0.0.0/0 | so anyone can reach the app |

Leave outbound at the default "all traffic". That is what lets the runner reach GitHub and
Docker Hub. **You do not need to open any inbound port for the pipeline itself** — the
runner dials out.

### On instance size and money

The first build of this used a `c7i-flex.large` at ~$0.08/hr. That is about 2 months of
running on $120 of credit. A `t3.micro` at ~$0.012/hr is roughly 10 months for the same
money. Use t3.micro.

If your account is on the AWS Free Plan, usage draws down **credits**, not money — and the
Bills page will read USD 0.00 the whole time, which is misleading. Watch the credit balance
instead:

```
https://console.aws.amazon.com/billing/home#/credits
```

Credit figures refresh only about every 24 hours, so today's usage will not appear until
tomorrow.

What actually consumes credit: compute while the instance runs, the EBS storage volume
**until the instance is terminated** (stopping is not enough), and any public IPv4 address
(~$0.005/hr).

**Checkpoint:** you can SSH in.

```bash
ssh -i your-key.pem ubuntu@YOUR-EC2-PUBLIC-IP
```

If it hangs, your security group is not allowing your IP on port 22. If it says permissions
are too open on the `.pem`, `chmod 400` it.

---

## Part 9: Install Docker on the server

SSH in, then:

```bash
sudo apt-get update -y
```

```bash
sudo apt-get upgrade
```

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
```

```bash
sudo sh get-docker.sh
```

```bash
sudo usermod -aG docker ubuntu
```

```bash
newgrp docker
```

The last two matter for the pipeline specifically. `usermod -aG docker ubuntu` adds the
`ubuntu` user to the docker group so it can run `docker` without `sudo` — and the runner
will be running as `ubuntu`. The deploy job's commands have no `sudo` in them. Skip this
and every deploy fails with a permission-denied error on the Docker socket.

`newgrp docker` applies the new group to your current shell without logging out.

**Order matters:** do this *before* installing the runner service in Part 10. If the
service was already installed, restart it afterwards, or it keeps the old group membership.

**Checkpoint:** this prints a "Hello from Docker!" message, with no `sudo`:

```bash
docker run hello-world
```

---

## Part 10: Turn the server into a self-hosted runner

This is the part that goes wrong. Read all of it before typing anything.

Go to your repo on GitHub: **Settings → Actions → Runners → New self-hosted runner**.
Choose **Linux / x64**. GitHub shows you a list of commands. Run them on the EC2 box, one
at a time.

### Trap 1: the `mkdir && cd` line

The first command is:

```bash
mkdir actions-runner && cd actions-runner
```

If that directory already exists — say you are retrying — `mkdir` **fails**, the `&&`
short-circuits, `cd` never runs, and the entire runner installs into your home directory
instead. Everything afterwards then refers to paths that do not exist. If you are retrying,
run the two commands separately, or delete the folder first.

### Trap 2: the label prompt

`./config.sh` asks you for "additional labels". **Press Enter. Take the default.**

The workflow says `runs-on: self-hosted`, which matches the default label. Type your own
label and every deploy job hangs on `Waiting for a runner to pick up this job...` forever,
with no error to read.

### Trap 3: the token expires

The registration token embedded in those commands is valid for about **one hour**. Reuse an
old one and `config.sh` fails with:

```
Http response code: NotFound from POST .../actions/runner-registration
```

That 404 means **expired token**. It does not mean a wrong URL or a missing repository.
Refresh the GitHub page to get a new token and run `config.sh` again.

### Trap 4 — the big one: do not use `./run.sh`

At the end, GitHub tells you to run:

```bash
./run.sh
```

**Do not.** It works — the runner comes online, your first deploy succeeds, and you feel
finished. Then you close your SSH window, and `run.sh` dies with the session.

The instance stays up. Port 22 answers. Port 8000 answers, because the container is still
running. Everything looks fine. But the runner is **Offline**, and every deploy from then on
sits in `Waiting for a runner to pick up this job...` indefinitely. Jobs do not fail — they
queue. There is no error to search for.

"The server is reachable" tells you nothing about whether the runner is alive.

Install it as a **service** instead, so it runs in the background and survives both logout
and reboot:

```bash
sudo ~/actions-runner/svc.sh install
```

```bash
sudo ~/actions-runner/svc.sh start
```

### If it ever shows Offline later

**Do not re-register.** Check whether the registration still exists:

```bash
ls -a ~/actions-runner/.runner ~/actions-runner/.credentials
```

If those two files are there, the registration is intact and only the process is missing.
Run the two `svc.sh` commands above. A job already sitting in the queue gets picked up
within seconds — no need to push again.

**Checkpoint:** Settings → Actions → Runners shows your runner as **Idle** with a green
dot. Not "Offline". Then reboot the box (`sudo reboot`), wait a minute, reload the page,
and confirm it comes back to Idle by itself. If it does not, the service did not install
and you are still on `run.sh`.

---

## Part 11: Add the secrets

Repo → Settings → Secrets and variables → Actions → New repository secret. One at a time.

### Required — the pipeline fails without these

| Secret | Value |
|---|---|
| `DOCKER_USERNAME` | your Docker Hub username, e.g. `zeeshanyq` |
| `DOCKER_PASSWORD` | the Docker Hub **access token** from Part 6.2 |
| `IMAGE_NAME` | the Docker Hub repo name, e.g. `agenticbot` |
| `GROQ_API_KEY` | your key |
| `TAVILY_API_KEY` | your key |
| `OPENWEATHER_API_KEY` | your key |
| `ALPHAVANTAGE_API_KEY` | your key |

### Optional — forwarded if set, ignored if not

| Secret | What you lose without it |
|---|---|
| `REGISTRY` | nothing; defaults to `docker.io` |
| `WEATHERSTACK_API_KEY` | the alternate weather source |
| `OPENROUTER_API_KEY` | OpenRouter free models disappear from the model picker |
| `OPENAI_API_KEY` | OpenAI models |
| `GOOGLE_API_KEY` | Gemini models fail when selected |
| `LANGSMITH_*` (4 of them) | tracing |

### What you do NOT need

**No AWS secrets.** Most tutorials tell you to add `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY` and `AWS_REGION`. Those belong to the ECR version of this
pipeline, where GitHub's machine has to authenticate into AWS. Ours does not — the deploy
runs on the box already, and the registry is Docker Hub. Nothing in the workflow reads
them. Adding them does no harm but leaves a long-lived AWS key sitting in GitHub for no
reason.

**Checkpoint:** the image path the workflow builds is
`docker.io/<DOCKER_USERNAME>/<IMAGE_NAME>`. Read your two secrets back and check that
string is exactly the Docker Hub repository you pushed to in Part 6. A typo here produces
a "repository does not exist" failure that reads like an auth problem.

---

## Part 12: The first deploy

```bash
git commit --allow-empty -m "Trigger first deploy"
```

```bash
git push
```

Open the **Actions** tab and watch. Expect roughly:

| Job | Time | What you should see |
|---|---|---|
| lint | ~30 s | green |
| build & push | 8–12 min first time | green. Later runs 1–2 min thanks to the cache |
| deploy | ~1 min | green, ending in "Chainlit application is healthy." |

If the deploy job sits on `Waiting for a runner...`, the runner is Offline. Go back to
Part 10.

**Checkpoint:** open `http://YOUR-EC2-PUBLIC-IP:8000`. Ask a question that uses a tool.

### Do not judge the deploy by the landing screen

The empty chat screen — "How can I help?", the tool badges, the four suggestion chips — is
**byte-identical** between versions of this app. It looks exactly the same whether you
deployed today's code or three-week-old code.

This has already cost about three round trips of confused debugging on a day when the
deploy had in fact succeeded every single time. Do not use it as evidence of anything.

Verify properly instead — SSH in and check the code actually inside the container:

```bash
docker exec dexter grep -c "Data_Agent" /app/dexter/backend.py
```

```bash
docker ps --filter "name=^dexter$"
```

```bash
docker logs --tail 100 dexter
```

Or by behaviour: ask for a chart, or upload a spreadsheet. Older versions of the backend
have no such code path at all, so it cannot fake a pass.

### One naming trap

The container **on the server** is called `dexter`. The examples in the README, for running
locally, use `agenticbot`. Copy a command from one context into the other and it silently
targets a container that does not exist — `docker logs agenticbot` on the server just says
"No such container" while the real app runs happily beside it.

---

## Part 13: The daily loop

From here on, shipping is:

```bash
git add .
```

```bash
git commit -m "Add whatever"
```

```bash
git push
```

Three to five minutes later it is live. That is the whole point of everything above.

### Rolling back

Every build also published an immutable `:sha-<short>` tag. To go back, SSH into the box:

```bash
docker stop dexter && docker rm dexter
```

```bash
docker run -d --restart unless-stopped --name dexter -p 8000:8000 --env-file .env zeeshanyq/agenticbot:sha-abc1234
```

Better still, `git revert` the bad commit and push — that puts the pipeline back in charge
and leaves the server matching the repository, which is the state everything else assumes.

### Redeploying without a code change

Actions tab → CI/CD Pipeline → **Run workflow**. That is what `workflow_dispatch` is for.
Use it after changing a secret, since secrets are read at deploy time.

### After changing a secret

A secret change does nothing until a new deploy runs. Trigger one with Run workflow.

---

## Part 14: When it breaks

Ordered by how often each one actually happens.

### Deploy job stuck on "Waiting for a runner to pick up this job..."

The runner is Offline. Ninety percent of the time it was started with `./run.sh` and the
SSH session closed. SSH in and:

```bash
sudo ~/actions-runner/svc.sh status
```

```bash
sudo ~/actions-runner/svc.sh start
```

Do not re-register. See Part 10.

The other cause is a custom runner label that does not match `runs-on: self-hosted`.

### `Error: Invalid API key`, but only in Docker

A key in `.env` was written as `KEY = value`. python-dotenv strips the space locally, so it
works outside Docker; Docker's `--env-file` parser passes the leading space through and the
API rejects the key. Remove all spaces around `=`. See the length-check command in Part 4.5.

### `invalid env file (.env): variable 'X ' contains whitespaces`

Same root cause, louder symptom. Trailing space before the `=`.

### Pipeline is green but the site does not load

Check in this order:

1. Is the container running? `docker ps` on the box.
2. Is port 8000 open to 0.0.0.0/0 in the security group? Easy to forget on a rebuilt box.
3. Does the Dockerfile `CMD` include `--host 0.0.0.0`? Without it the container
   is healthy and reachable by nothing.

### `Could not open requirements file: requirements.txt` during the build

The Dockerfile is copying files in the wrong order. `COPY deploy/requirements.txt` must come
before the `pip install`, and the `dexter/` copy must come after it.

### Build succeeds locally, fails in Actions

Something is on your machine but not in the repo. Usually a file that `.gitignore` or
`.dockerignore` is excluding, or a dependency you installed with `uv add` and never mirrored
into `requirements.txt`.

### `ports are not available: ... bind: Only one usage of each socket address`

Local only. Something already holds that port, usually your own Chainlit process.

```bash
netstat -ano | grep ":8000"
```

Then either publish a different host port (`-p 8001:8000`) or stop the process with
`taskkill //PID <pid> //F`.

### Disk full on the server

Old image tags. The prune step should handle it; to do it by hand:

```bash
docker system df
```

```bash
docker image prune -af --filter "until=24h"
```

```bash
df -h /
```

### Image is enormous (6–7 GB)

The CPU-only torch index line is missing from the top of `requirements.txt`. See Part 4.1.

### The runner is fine but the deploy fails on `docker: permission denied`

`usermod -aG docker ubuntu` never ran, or the runner service was installed before it did.
Run it, then restart the service.

---

## Part 15: Shutting it down

**Stopping an instance does not stop the storage bill.** The EBS volume keeps consuming
credit until the instance is **terminated**.

To take everything down:

1. On the box: `docker rm -f dexter`
2. Console → EC2 → Instances → select → Instance state → **Terminate**
3. Confirm the attached volume is deleted with it
4. Release any Elastic IP separately — an unattached Elastic IP bills on its own
5. Optional: remove the runner from Settings → Actions → Runners

Your Docker Hub image survives all of this. Rebuilding later is Parts 8, 9, 10 and 11
again — the code and the pipeline need no changes at all. A rebuilt instance gets a
**different public IP**.

### Where this project stands right now

The EC2 instance was **terminated on 2026-08-25** to stop charges, along with its 32 GiB
volume. There were never any Elastic IPs or snapshots. The former public address is not
recorded here. Nothing is running; nothing is billing.

The full pipeline was proven working end to end earlier that same day, before teardown.

| Thing | Value |
|---|---|
| GitHub repo | https://github.com/Zeeshanx186/Agentic-Chatbot-Using-LangGraph |
| Docker Hub image | `zeeshanyq/agenticbot` — still published |
| Container name on the server | `dexter` |
| Tag the deploy pulls | `:main` |
| Region | eu-north-1 (Stockholm) |

---

## Appendix A: Every file, ready to copy

Four files. Copy them into a new project, change the names, and the pipeline works.

### `Dockerfile`

See [Part 4.2](#42-the-dockerfile), or the live file: [../deploy/Dockerfile](../deploy/Dockerfile).

### `.dockerignore`

See [Part 4.3](#43-write-dockerignore), or [../.dockerignore](../.dockerignore).

### `.github/workflows/cicd.yaml`

The live, working file: [../.github/workflows/cicd.yaml](../.github/workflows/cicd.yaml). Copy it
whole. The only things to change for a new project are the secret names in the
`Validate application secrets` step and the `-e` flags in the `docker run` step — those are
this app's API keys.

Two values are worth deciding consciously when you reuse it:

- The container name, `dexter`, appears in five steps. Change it everywhere or nowhere.
- The port, `8000`, appears in the `docker run` publish, in the health check URL, and in
  your security group. All three must agree.

### `.env` (never committed)

```
GROQ_API_KEY=
TAVILY_API_KEY=
OPENWEATHER_API_KEY=
ALPHAVANTAGE_API_KEY=
```

No spaces around `=`.

---

## Appendix B: Glossary

| Term | Plain meaning |
|---|---|
| **CI** | Continuous Integration. Automatically check every push — lint, tests, build |
| **CD** | Continuous Deployment. Automatically ship what passed |
| **Image** | A frozen snapshot: your code plus everything it needs to run. A recipe |
| **Container** | A running instance of an image. The meal |
| **Registry** | Where images are stored and shared. Docker Hub, ECR, GHCR |
| **Tag** | A label on an image, after the colon. `agenticbot:main` |
| **Layer** | One step of a Dockerfile. Cached individually — this is why file order matters |
| **Runner** | The machine that executes a job. GitHub's, or your own ("self-hosted") |
| **Job** | One block of a workflow. Jobs can depend on each other with `needs:` |
| **Workflow** | The whole `.yaml` file and everything it triggers |
| **Secret** | An encrypted value stored in GitHub, injected at run time, masked in logs |
| **EC2** | An AWS virtual machine |
| **AMI** | The OS image an EC2 instance boots from |
| **Security group** | The firewall around an EC2 instance |
| **EBS** | The disk attached to an EC2 instance. Bills separately, until *terminated* |
| **Health check** | A request that succeeds only when the app is truly ready. This image answers `GET /` |

---

## Appendix C: Doing this on a brand new project

The shortest correct path, assuming a different app entirely:

1. Get the app running locally. Know which port it listens on.
2. Write `requirements.txt` (or the equivalent for your language).
3. Write the `Dockerfile`. Dependencies copied and installed **before** the code.
   Bind to `0.0.0.0`, not localhost.
4. Write `.dockerignore`. Make sure `.env` is in it.
5. Build and run locally on a spare port. Confirm it works in the container, not just on
   your machine.
6. Push to a new GitHub repo. Confirm `.env` did not go with it.
7. Create the Docker Hub repo and an access token. Push once by hand to prove the path.
8. Copy `cicd.yaml` in. Change: the container name, the port (three places), and the list
   of application secrets.
9. Launch a t3.micro, open port 22 to your IP and your app port to the world.
10. Install Docker. `usermod -aG docker ubuntu`.
11. Register the runner, take the **default label**, install it as a **service**.
12. Add the secrets in GitHub.
13. Push. Watch the Actions tab.
14. Verify by something that differs between versions — never by the landing screen.

The three failures that will cost you the most time, in order:

1. The runner started with `./run.sh` and silently went Offline (Part 10).
2. `.env` written with spaces around `=`, so keys work locally and fail in Docker (Part 4).
3. Missing `--host 0.0.0.0`, so the container is healthy and unreachable (Part 4.2).
