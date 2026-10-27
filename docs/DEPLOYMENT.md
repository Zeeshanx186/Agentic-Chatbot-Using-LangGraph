# Agentic-Chatbot-APP-CICD-Deployment-with-Github-Actions-on-AWS

Description: About the deployment

```
1. Build docker image of the source code
2. Push your docker image to docker hub
3. Launch Your EC2
4. Pull Your image from docker hub in EC2
5. Lauch your docker image in EC2
```

Steps 1 and 2 run on GitHub's `ubuntu-latest` runners. Steps 4 and 5 run **on the EC2
box itself**, through a self-hosted runner that polls GitHub for work. There is no SSH
step and no private key stored in GitHub.

Pipeline file: [../.github/workflows/cicd.yaml](../.github/workflows/cicd.yaml)

| Job | Runs on | Does |
|---|---|---|
| `lint` | ubuntu-latest | flake8. Hard-fails on `E9,F63,F7,F82`; style report advisory |
| `push_to_registry` | ubuntu-latest | Buildx build, push to Docker Hub, registry build cache |
| `continuous_deployment` | self-hosted (EC2) | Validate secrets, pull `:main`, replace container `dexter`, health check, prune |

Triggers: push to `main`, pull requests to `main`, manual `workflow_dispatch`.
Pull requests build the image to prove the Dockerfile still works, but never log in,
never push, and never deploy. Only `main` publishes and deploys.

---

## 1. Login to AWS console.

Region used for this project: **eu-north-1 (Stockholm)**. The account id is intentionally not recorded here.

Billing note: this account is on the AWS Free Plan, so usage draws down credits rather
than money. Watch the credit balance at
`https://console.aws.amazon.com/billing/home#/credits` — the Bills page reads USD 0.00
even while credits are being consumed.

---

## 2. Create IAM user for deployment

```
#Policy:
1. AmazonEC2FullAccess
```

This is for a human logging into the console to launch and terminate instances.

**This pipeline does not need AWS credentials in GitHub.** The tutorial's
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_REGION` secrets belong to the ECR
variant of this deployment, where GitHub's hosted runner has to authenticate into AWS.
Here the registry is Docker Hub and the deploy job runs on the EC2 box already, so
nothing in [../.github/workflows/cicd.yaml](../.github/workflows/cicd.yaml) reads those three
values. Adding them changes no behaviour, but a long-lived access key sitting in GitHub
is a real credential to lose. Skip them.

---

## 3. Create EC2 machine (Ubuntu)

- AMI: Ubuntu
- Instance type: **t3.micro** is enough and is what to use. The original build ran on
  `c7i-flex.large` (~$0.08/hr, about 2 months on $120 of credit). t3.micro is
  ~$0.012/hr — roughly 10 months for the same credit.
- Storage: 30–32 GiB gp3. The image is ~2 GB per tag and old tags accumulate.
- Security group inbound rules:

| Port | Source | Why |
|---|---|---|
| 22 | your IP | SSH |
| 8000 | 0.0.0.0/0 | Chainlit |

Outbound stays default (all traffic) — the runner polls GitHub and the box pulls from
Docker Hub.

The app is reached at `http://<EC2-public-IP>:8000`.

---

## 4. Open EC2 and Install docker in EC2 Machine:

```
#optinal

sudo apt-get update -y

sudo apt-get upgrade

#Install Docker

curl -fsSL https://get.docker.com -o get-docker.sh

sudo sh get-docker.sh

sudo usermod -aG docker ubuntu

newgrp docker
```

Note: Do the port mapping to this port:- 8000

`usermod -aG docker ubuntu` is what lets the runner call `docker` without `sudo`. If the
runner service was installed *before* this ran, restart the service or the deploy job
fails with a permission-denied error on the Docker socket.

---

## 5. Configure EC2 as self-hosted runner:

```
setting>actions>runner>new self hosted runner> choose os> then run command one by one
```

Take the commands from that page — they carry a registration token that is only valid
for about an hour.

Two traps that cost real time on the first build:

**`mkdir actions-runner && cd actions-runner`** — when the directory already exists,
`mkdir` fails, `&&` short-circuits, `cd` never runs, and the entire runner installs into
the home directory instead.

**Leave the runner label at its default.** The workflow says `runs-on: self-hosted`.
Typing a custom label at the `config.sh` prompt makes every deploy job hang on
`Waiting for a runner to pick up this job...`.

### Install it as a service, not `./run.sh`

`./run.sh` is tied to the SSH session and dies when the terminal closes. The instance
stays up — port 22 and port 8000 keep answering — while the runner shows **Offline** and
every deploy queues forever. "The server is reachable" proves nothing about the runner.

```bash
sudo ~/actions-runner/svc.sh install
```

```bash
sudo ~/actions-runner/svc.sh start
```

The service is enabled, so it also survives reboots. A job already queued gets picked up
within seconds; no re-push needed.

### If the runner shows Offline

Do **not** re-register. If `~/actions-runner/.runner` and `.credentials` exist, the
registration is intact and only the process is missing — run the two `svc.sh` commands
above.

Re-running `config.sh` with an expired token fails as
`Http response code: NotFound from POST .../actions/runner-registration`. That 404 means
*expired token*, not a wrong URL or a missing repo.

---

## 6. How to add secret keys to GitHub Actions:

Settings → Secrets and variables → Actions → New repository secret.

### Required — the pipeline fails without these

| Secret | Value used here | Read by |
|---|---|---|
| `DOCKER_USERNAME` | `zeeshanyq` | build + deploy login |
| `DOCKER_PASSWORD` | Docker Hub **access token**, not the account password | build + deploy login |
| `IMAGE_NAME` | `agenticbot` | image path in both jobs |
| `GROQ_API_KEY` | Groq key | main LLM provider |
| `TAVILY_API_KEY` | Tavily key | `web_search` tool |
| `OPENWEATHER_API_KEY` | OpenWeather key | `get_weather` tool |
| `ALPHAVANTAGE_API_KEY` | AlphaVantage key | `get_stock_price`, `get_stock_history` |

The deploy job checks those four application keys in a `Validate application secrets`
step **before** it touches the running container. A missing key otherwise produces a
container that starts clean, looks healthy, and fails on every tool call at request time.

### Optional — forwarded if set, ignored if not

| Secret | Effect when missing |
|---|---|
| `REGISTRY` | Defaults to `docker.io` |
| `WEATHERSTACK_API_KEY` | Alternate weather source unavailable |
| `OPENROUTER_API_KEY` | OpenRouter free models drop out of the model picker |
| `OPENAI_API_KEY` | OpenAI models unavailable |
| `GOOGLE_API_KEY` | Gemini models in the picker fail when selected |
| `LANGSMITH_TRACING` | No tracing |
| `LANGSMITH_ENDPOINT` | No tracing |
| `LANGSMITH_API_KEY` | No tracing |
| `LANGSMITH_PROJECT` | Traces land in the default project |

The full image path is assembled as
`${REGISTRY or docker.io}/${DOCKER_USERNAME}/${IMAGE_NAME}` — here
`docker.io/zeeshanyq/agenticbot`.

Secrets reach the container as bare `-e VAR` with no `=`, which forwards the value from
the step's environment. No secret is ever written into a command line or into the run log.

---

## 7. The image being deployed

[../deploy/Dockerfile](../deploy/Dockerfile). The build context is the repository root.

| Piece | Why it is there |
|---|---|
| `FROM python:3.11-slim` | Debian Trixie, still receiving security updates. Was `slim-buster`, which is end-of-life |
| `PYTHONDONTWRITEBYTECODE=1` | No `.pyc` files written into the container layer |
| `PYTHONUNBUFFERED=1` | Chainlit and tool logs appear in `docker logs` immediately instead of sitting in a buffer |
| `PIP_NO_CACHE_DIR=1` | pip's wheel cache never lands in the image |
| `build-essential` | Compiles any dependency without a prebuilt wheel for this platform |
| `libgomp1` | OpenMP runtime, required by `faiss-cpu` at import |
| `libgl1`, `libglib2.0-0` | OCR / OpenCV runtime libraries |
| `COPY deploy/requirements.txt` before the app code | The dependency layer stays cached across code edits |
| `COPY dexter/` | Application package, including `google_tools.py` |
| non-root `USER dexter` | The process does not run as root |
| `EXPOSE 8000` | Documents the port; the publish is `-p 8000:8000` at run time |
| `chainlit run ... --host 0.0.0.0 --port 8000 --headless` | Bind all interfaces, no browser launch, no first-run prompt |

Without `--host 0.0.0.0`, the container starts, `docker ps` shows it healthy,
and `http://<EC2-IP>:8000` refuses the connection.

The `--extra-index-url https://download.pytorch.org/whl/cpu` line at the top of
[../deploy/requirements.txt](../deploy/requirements.txt) is what keeps the image around 2 GB instead of
6–7 GB. The default torch wheel is the CUDA build and drags in several GB of NVIDIA
libraries the container has no GPU to use.

---

## 8. What the deploy job actually does

On the EC2 box, in order:

1. **Log in to Docker Hub** — so the box can pull the image.
2. **Validate application secrets** — fails the run before anything is stopped.
3. **Pull** `docker.io/zeeshanyq/agenticbot:main`.
4. **Stop and remove** the existing container `dexter`. Both commands end in `|| echo`,
   so a first-ever deploy with no container present does not fail.
5. **Run** the new container:

```bash
docker run -d --restart unless-stopped --name dexter -p 8000:8000 -e GROQ_API_KEY -e TAVILY_API_KEY "$IMAGE_PATH:main"
```

6. **Verify container is running** — `docker inspect -f '{{.State.Running}}' dexter`, and
   dump the last 100 log lines if not.
7. **Verify the Chainlit HTTP check** — polls `http://localhost:8000/` from inside the
   container, 12 attempts, 5 s apart. The process can exist before Chainlit is accepting
   connections, and a config crash can land later still. Embeddings and OCR load on first
   use, not at import.
8. **Clean up old images** — `docker image prune -af --filter "until=24h"`, with
   `if: always()` so a red pipeline cannot fill the root volume.

### Image tags

Produced by `docker/metadata-action`:

| Tag | From | Purpose |
|---|---|---|
| `:main` | `type=ref,event=branch` | **What the deploy pulls** |
| `:sha-<short>` | `type=sha,format=short` | Immutable, for pinning or manual rollback |
| `:latest` | `type=raw`, default branch only | Local convenience only |

The deploy pulls **`:main`, not `:latest`**. Renaming the branch or changing the tagging
rules breaks the pull.

Concurrency is `group: cicd-${{ github.ref }}` with `cancel-in-progress: false`. The
self-hosted runner is one machine — two deploys at once would fight over the same
container name and the same port, so they queue instead, and a deploy already replacing
the running container is never cancelled.

---

## 9. Verifying a deploy actually took effect

**Never judge this from the landing screen alone.** The welcome screen can look the same
across releases.

Check one of these instead:

```bash
docker exec dexter grep -c "Data_Agent" /app/dexter/backend.py
```

```bash
docker ps --filter "name=^dexter$"
```

```bash
docker logs --tail 100 dexter
```

Or by behaviour: ask for a chart, or upload a spreadsheet. Neither path exists in the
older backend at all.

Note the container on the server is named **`dexter`**. Local `docker run` examples in
[../README.md](../README.md) may use another container name. Docker commands copied between
the two contexts target nothing if the name does not match.

---

## 10. Teardown

Stopping an instance does **not** stop the EBS volume from consuming credit. Terminate it.

```bash
docker rm -f dexter
```

Then in the console: EC2 → Instances → select → Instance state → **Terminate**. Confirm
the attached volume is deleted with it, and release any Elastic IP separately — an
unattached Elastic IP bills on its own.

What consumes credit: compute while running, the EBS volume until the instance is
*terminated*, and any public IPv4 (~$0.005/hr).

The Docker Hub image survives teardown. A rebuild is: launch instance → install Docker →
register runner as a service → push to `main`. A rebuilt instance gets a **different
public IP**.

---

## 11. Current status

**Terminated 2026-08-25.** The EC2 instance in eu-north-1c (c7i-flex.large, 32 GiB gp3)
is gone, along with its volume. No Elastic IPs and no snapshots were left behind. The
former public address is not recorded here. Nothing from that instance is running, and
nothing from it is billing.

The full pipeline was proven working end to end earlier that same day, before teardown.

| Thing | Value |
|---|---|
| GitHub repo | https://github.com/Zeeshanx186/Agentic-Chatbot-Using-LangGraph |
| Docker Hub image | `zeeshanyq/agenticbot` — still published |
| Container name on the server | `dexter` |
| Tag the deploy pulls | `:main` |
| Region | eu-north-1 (Stockholm) |
