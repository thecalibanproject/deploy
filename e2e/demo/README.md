# Recorded product demos

A framework that records Caliban the way a customer experiences it: an operator sets a scenario up in
the web console, a client sends real requests through the gateway, the console shows what happened,
and a check script proves the outcome. Every scenario is recorded as a 1920x1080 browser video with
a caption overlay; a terminal recording covers the gateway host; one stitch step builds a video per
scenario and one full video with chapter markers.

Health triage (`e2e/health-triage/demo/scenario.py`) is the reference scenario.

```
e2e/demo/
├── README.md        this contract
├── framework.py     Ctx, the recorder (Python Playwright, sync API), the scenario loader
├── overlay.js       injected caption bar, cursor, focus ring, client panel, terminal card
├── run-demo.sh      the runner on the gateway host: pull | infra | record | check | cards | stitch | all
├── infra.sh         the gateway-host commands of the infrastructure terminal segment
├── record-term.sh   records a command with `script`, renders it with agg and ffmpeg
├── cast.py          `script` timing to asciicast v2
├── cards.py         title and section cards, the health results segment
├── stitch.py        per-scenario MP4s and the full MP4 with chapters
├── telemetry.py     Server-Timing parsing, the telemetry records and the combined report
├── test_telemetry.py  its unit tests (python3 -m unittest discover -s e2e/demo -p 'test_*.py')
└── images.env       the pinned images
e2e/<scenario>/
├── demo/scenario.py NAME, TITLE, chapters(ctx)
├── seed.py          optional: seeds the scenario's own MongoDB database before recording
└── check.py         asserts the outcome after recording; non-zero exit on failure
```

## Images (pinned)

| Image | Use |
|---|---|
| `mcr.microsoft.com/playwright/python:v1.63.0-noble` | Python 3.12, Playwright 1.63.0 and Chromium: every browser recording and the cards |
| `ghcr.io/asciinema/agg:1.9.0` | asciicast to GIF (terminal segments) |
| `jrottenberg/ffmpeg:8.0.3-ubuntu2404` | GIF and WebM to MP4, idle cuts, concatenation, chapters |

`images.env` holds them; `run-demo.sh pull` pulls them. Scenario code runs inside the Playwright
image: Python 3.12 standard library plus `playwright` (sync API). Nothing else is installed.

## The scenario contract

`e2e/<scenario>/demo/scenario.py`:

```python
import framework                     # e2e/demo is on sys.path

NAME = "law"                         # the directory name under e2e/, used in file names
TITLE = "Contract review for a law firm"

def chapters(ctx):                   # -> list[tuple[str, Callable[[Ctx], None]]]
    return [("tenant", tenant), ("requests", requests)]

def tenant(ctx):
    ctx.caption("Tenant", "Create the tenant: the isolation boundary.")
    ctx.goto("/tenants")
    ...
```

The runner signs in before the first chapter (the sign-in is recorded) and records each chapter
as a timeline chapter named by its key. A chapter that raises stops the scenario: the failure is
in the timeline (`failed`), a screenshot `zz-failure.png` is taken, the video is kept, and the
runner reports it.

### `Ctx` (e2e/demo/framework.py)

| Member | What it does |
|---|---|
| `ctx.page` | The Playwright `Page`, signed in to the console as the operator admin (admin token) |
| `ctx.console_url` | The console's base URL (the control plane, `E2E_CP_URL`) |
| `ctx.gateway_url` | The data plane's base URL (`E2E_ROUTER_URL`) |
| `ctx.caption(title, line, stage=None, note=None)` | The caption bar: a chapter title, one line on what Caliban does, an optional stage chip (`Clear`, `Route`, `Screen`, `Cache`, `Ground`, `Land`, `Log`, or `Nodes`, `Evals`) and an optional note line. Stays until the next caption. Does not pause |
| `ctx.api_caption(title, line, what, stage=None)` | For a step the console has no page for: the caption plus the note "Admin API, no console page for this yet: <what>"; the step is listed in the timeline's `api_steps` (the report of console gaps) |
| `ctx.pause(seconds)` | Pacing for the viewer only (scaled by `DEMO_PACE`), never for correctness |
| `ctx.api` | Admin API client: `get/post/put/patch/delete(path, json=None, tenant_key=None)` returns the parsed JSON (None for an empty body) and raises `framework.ApiError` (`.status`, `.body`) on a non-2xx answer. Uses `CALIBAN_ADMIN_TOKEN`; `tenant_key` sends that bearer instead. `path` starts with `/api/v1/` |
| `ctx.gateway(path, json, api_key, headers=None, label=None)` | Data-plane call: POST with `json`, GET when `json` is None. Returns `(status, headers, body)`, headers lower-cased, body parsed when JSON. Never raises on a status. Always records telemetry (below); `label` names the request in it (default: the chapter key) |
| `ctx.client_panel(title, request_json, status, headers, body)` | Full-screen "customer app" overlay on the same page (one continuous video): the request, the status, the `x-caliban-*` and `caliban-*` headers, the answer (`choices[0].message.content`, or `body` itself), and the timing bar of the last `ctx.gateway` call (`timing=` overrides it) with the node steps' durations when it started a node run. `status=None` shows the request as pending. Stays until `ctx.hide_panel()`, `ctx.goto()` or the next panel. The API key is never shown |
| `ctx.run_timing(run_id, api_key, label=None, end_to_end_ms=None)` | Records a node run's step durations once it has ended, for runs that ended after the call that started them (a human step, a sync wait that ran out). Returns the run |
| `ctx.record_eval(label, eval)` | Records an eval's latency percentiles (`summary.latency_ms` p50 and p95) in the telemetry report |
| `ctx.hide_panel()` | Hides the client panel or terminal card |
| `ctx.terminal(title, lines)` | A terminal-looking card, typed out: lines starting with `$ ` are typed as commands, the others printed as output. Returns when the typing ends; stays until `ctx.hide_panel()` |
| `ctx.goto(route)` | Console hash route, such as `/tenants/acme?tab=intents`; waits for the shell |
| `ctx.screenshot(name)` | `shots/<scenario>/<name>.png` |
| `ctx.env` | The `E2E_*` and `DEMO_*` variables plus those named in `DEMO_PASS_ENV` |
| `ctx.mark(event)` | A timeline event at the current time |

Additive helpers (use them for a watchable recording; all wait on UI state, never on time):

| Member | What it does |
|---|---|
| `ctx.point(locator, hold=0.7)` | Scrolls to it, moves the visible cursor there, rings it |
| `ctx.click(locator)` | Waits until enabled, points, clicks |
| `ctx.type(locator, text)` | Points, types short text key by key (long text is filled at once) |
| `ctx.nav(label)` | Clicks a sidebar entry (`Tenants`, `Models`, `Datasources`, `Ontology`, `Nodes`, `Runs`, `Retrieval`, `Usage`, `Audit`, ...) |
| `ctx.dialog(name)` | The open dialog or drawer with that title, once visible |
| `ctx.idle(label)` | Context manager around a long wait (evals, indexing): the stitch shortens it to its first and last seconds; pass `clock=True` to `caption` to show the real time elapsed |
| `ctx.card(html)` / `ctx.hide_card()` | Full-screen title card on the page |
| `ctx.work_dir`, `ctx.out_dir` | `$E2E_WORK_DIR` and `$DEMO_OUT` |

Console selectors: every form control is labelled (`page.get_by_label("Name")`), dialogs and drawers
are `role=dialog` named by their title, tables are named (`get_by_role("table", name="Datasources")`),
and forms have `aria-label`s (`get_by_role("form", name="Register datasource")`). Read the console's
README for the pages and labels; the health scenario uses most of them. A newly minted API key is
blurred in every recording (`[data-testid=plaintext-key]`).

### Telemetry

`ctx.gateway` times every data-plane call on the client (to the response headers, to the first body
byte, total) and parses the W3C `Server-Timing` response header when the gateway sends one (core's
stage metrics: `auth`, `route` with `desc` `pinned`, `rules`, `knn` or `keyword`, `embed`, `pii`,
`cache` with `desc` `hit` or `miss`, `upstream` (time to first byte), `upstream_total`, `node`,
`total`). Classification is `route` alone: `embed`, the routing embedder call, is
measured inside it and shown as its part ("12.5 (embed 9.1)"). Without the header only the
client-side times are shown and reported. When a call
started a node run that has ended (the `caliban-run-id` header, or the `id` of a
`/v1/nodes/{name}/runs` answer), the run and its steps' `duration_ms` are fetched with the same key.

Each call appends a record to `$DEMO_OUT/<scenario>-telemetry.jsonl` (`kind`: `request`, `run` or
`eval`). After a scenario's last chapter the framework adds a **Telemetry** chapter: per request the
intent, the route stage, classification, PII, cache, model TTFB and total, node steps and end to end,
then p50 and p95 per stage and per node step, and the eval latencies recorded with
`ctx.record_eval`. A scenario may append records of its own with `telemetry.append(scenario, record)`
(`kind: request` with `label`, `server` as `parse_server_timing` returns it, `client` with
`total_ms`), such as an SDK's classification rows. `telemetry.py report` writes the combined `telemetry.md`, `telemetry.csv` and
`telemetry.json` across scenarios; `cards.py telemetry` renders them as the full video's closing
segment.

### Rules for scenarios

- One tenant per scenario, set up through the console; the admin API only where the console has no
  page, always with `ctx.api_caption` so the video says so and the report lists the gap.
- Captions make true claims only, in the website's words for the request path (Clear, Route,
  Screen, Cache, Ground, Land, Log). Show outcomes with captions built from what the API answered,
  not from what should happen.
- No secrets on screen: tokens and passwords go into password fields or not at all.
- No em dashes anywhere, captions included.
- Synthetic data only.

## The runner

On the gateway host, after `e2e/health-triage/e2e.sh up` (the shared stack: Caliban, the MongoDB
replica set, the model recorder or the scripted model):

```sh
e2e/demo/run-demo.sh all                   # pull, infra, then every scenario: seed, record, check; cards, stitch
e2e/demo/run-demo.sh record health-triage law
e2e/demo/run-demo.sh check health-triage
e2e/demo/run-demo.sh stitch
```

`all` records the use cases in `DEMO_ORDER` (health-triage, law, developer, fintech), then any
other scenario. `stack.sh` (sourced by the runner; source it yourself before `e2e.sh up`, `exec` or
`down` of a demo stack) adds what the scenarios need to the shared stack through e2e.sh's settings:
`E2E_EXTRA_COMPOSE` (colon separated; on the testbed the gateway's compose file first, then the
fintech overlay), `E2E_EXTRA_SERVICES` (the refund system's MCP server), `E2E_EXTRA_CONFIG` (Cedar
Bank's quotas, appended to the rendered `caliban.toml`) and `E2E_WORK_SUBDIRS`. The developer
scenario installs the Python SDK from `DEV_SDK_SRC`; a wheel on the host is mounted into the
Playwright container with `DEMO_MOUNTS` (colon separated host paths, read-only). `DEV_*` and
`FINTECH_*` settings reach the scenarios like `E2E_*` and `DEMO_*`.

For each scenario: `e2e/<scenario>/seed.py` when it exists, the recording, then `e2e/<scenario>/check.py`
recorded as a terminal segment (its exit status is the scenario's verdict). The environment is the
shared stack's (`e2e.sh exec`): `CALIBAN_ADMIN_TOKEN`, `E2E_CP_URL`, `E2E_ROUTER_URL`, `E2E_WORK_DIR`,
the MongoDB reader and root credentials for seeds and checks, and every `E2E_*` setting.

Outputs in `$E2E_WORK_DIR/demo/` (`e2e/health-triage/.work/demo/` by default):

| File | What |
|---|---|
| `<scenario>.webm` | The console recording, at the viewport's size: 1536x864 CSS pixels at the default `DEMO_SCALE=1.25` (larger text; the MP4s scale it to 1920x1080), 1920x1080 with `DEMO_SCALE=1` |
| `<scenario>.timeline.json` | Chapters (`key`, `title`, `start`, `end` in seconds of the video), `idle` spans, `marks`, `api_steps`, `failed` |
| `shots/<scenario>/NN-<chapter>.png` | A screenshot at the end of each chapter |
| `term/*.mp4`, `term/*.cast` | Terminal segments (`infra-up`, `check-<scenario>`), replayable casts |
| `<scenario>.mp4` | Per scenario: its title card, its console walkthrough, its checks |
| `<scenario>-telemetry.jsonl` | Every data-plane call's timings, node runs' step durations, eval latencies |
| `telemetry.md`, `telemetry.csv`, `telemetry.json` | The combined telemetry report |
| `caliban-demo.mp4` | Everything: title, the gateway host, every scenario, the telemetry segment, end card; chapter markers per part and per chapter |

## Testing without Docker

`DEMO_LOCAL=1` runs the Python steps with `DEMO_PYTHON` (a Python with `playwright==1.63.0` and its
Chromium) and the media steps with local `DEMO_AGG` and `DEMO_FFMPEG` binaries. See
`e2e/health-triage/RUN-ON-TESTBED.md` for the testbed run.
