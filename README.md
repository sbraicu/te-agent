# ThousandEyes Root Cause Analysis Agent

A self-contained, playbook-driven agent that connects to any ThousandEyes account via the [ThousandEyes MCP Server](https://docs.thousandeyes.com/product-documentation/integration-guides/thousandeyes-mcp-server), automatically triages active issues, and performs deep root cause analysis.

Designed to work with small/local LLMs — the investigation logic lives in deterministic playbooks, not in the model's reasoning.

---

## Table of Contents

- [Quick Start](#quick-start)
- [How It Works](#how-it-works)
- [Investigation Flow](#investigation-flow)
- [Playbooks](#playbooks)
- [Fault Isolation Logic](#fault-isolation-logic)
- [Configuration](#configuration)
- [Usage](#usage)
- [Adding Custom Playbooks](#adding-custom-playbooks)
- [Project Structure](#project-structure)
- [Design Decisions](#design-decisions)

---

## Quick Start

```bash
# 1. Clone / navigate to the project
cd /opt/bb/work/projects/AI/thousandeyes-rca-agent

# 2. Set up credentials
cp .env.example .env
# Edit .env — add your ThousandEyes API token and LLM endpoint details

# 3. Run with Docker
./run.sh docker

# Or run locally (Python 3.11+, Node.js 18+ required)
./run.sh local
```

---

## How It Works

Traditional approach: give a powerful LLM all the tools and hope it figures out what to investigate. This fails because:
- Raw MCP responses can be 50K+ tokens each — a single deep investigation blows context windows
- Small/local LLMs can't reason over massive unstructured API dumps
- The investigation strategy changes every time, making results inconsistent

Our approach: **playbooks encode the investigation strategy, the LLM is just a worker.**

```
┌──────────────────────────────────────────────────────────────────┐
│                    What the LLM does at each step                │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. CLASSIFY  — "This alert is an HTTP error" (trivial)          │
│  2. EXTRACT   — "Pull these 5 facts from this API response"      │
│  3. FOLLOW RULES — "All agents affected → target-side issue"     │
│  4. SYNTHESIZE — "Given these 10 bullet points, write the RCA"   │
│                                                                  │
│  At no point does the LLM decide WHAT to investigate.            │
│  The playbook decides. The LLM executes.                         │
└──────────────────────────────────────────────────────────────────┘
```

Each MCP tool response (potentially 50K tokens of raw JSON) is immediately compressed to ~5 lines by the LLM before moving to the next step. The final synthesis step only sees these summaries, keeping total context under 4K tokens.

---

## Investigation Flow

Every investigation follows this sequence:

```
Phase 0: Discover (once per session)
│  Learn the environment — what tests exist, what types,
│  which account groups, whether endpoint agents are deployed.
│
▼
Phase 1: Triage (every investigation)
│  List all active events, alerts, and outages.
│  Classify each by category, severity, blast radius.
│  Identify correlations (same time? same target? same provider?).
│
▼
Phase 2: Route (automatic)
│  Based on triage classification, pick the right playbook(s):
│
│  ┌─ HTTP errors on http-server test?     → http_error
│  ├─ DNS resolution failure?              → dns_failure
│  ├─ SSL/TLS certificate issue?           → ssl_tls
│  ├─ Target unreachable / 100% loss?      → connectivity
│  ├─ Latency or response time spike?      → latency
│  ├─ BGP reachability drop / route change? → bgp
│  ├─ Endpoint agent degradation?          → endpoint
│  └─ 3+ tests failing simultaneously?    → multi_test
│
▼
Phase 3: Investigate (per playbook)
│  Run each step: call MCP tool → compress response → next step.
│  Includes a fault isolation step with deterministic rules.
│  Final step: synthesize all findings into structured RCA.
│
▼
Output: Root Cause Analysis Report
   - Investigation Summary (trigger, time, scope)
   - Evidence (one line per data source)
   - Fault Isolation (layer, location, scope)
   - Root Cause (cause, confidence, evidence, alternatives)
   - Recommended Actions (immediate + follow-up)
```

---

## Playbooks

All playbooks are defined in `playbooks.yaml`. Each is a sequence of steps that call specific ThousandEyes MCP tools.

| Playbook | When It Runs | Steps | What It Checks |
|---|---|---|---|
| `discover` | Once per session | 4 | Account groups → all tests → endpoint agents → environment profile |
| `triage` | Every investigation | 4 | Events → alerts → outages → classify & prioritize |
| `http_error` | HTTP 4xx/5xx, timeouts | 10 | Alert → test config → availability → TTFB → loss → latency → fault isolation → path viz → anomalies → RCA |
| `dns_failure` | DNS resolution errors | 6 | Alert → test config → DNS metrics → fault isolation → path to DNS → RCA |
| `ssl_tls` | Certificate/handshake errors | 4 | Alert → test config → availability → RCA |
| `connectivity` | Target unreachable | 8 | Test config → events → loss → latency → full path viz → BGP → outages → RCA |
| `latency` | Performance degradation | 7 | Test config → latency → TTFB → loss → path viz → anomalies → RCA |
| `bgp` | Routing anomalies | 5 | BGP results → route details → path impact → outages → RCA |
| `endpoint` | End-user experience | 4 | List agents → metrics → events → RCA |
| `multi_test` | 3+ tests failing together | 4 | Test details → all alerts → outages → common root cause |

---

## Fault Isolation Logic

Each investigation playbook includes a fault isolation step with deterministic rules. The LLM follows these rules rather than guessing:

### By Blast Radius
| Pattern | Conclusion |
|---|---|
| ALL agents affected, network healthy | Target-side issue (server down, auth failure, cert expired) |
| ALL agents affected, high loss/timeout | Target-side OR upstream provider outage |
| SOME agents affected (same region) | Regional or ISP-specific issue |
| SOME agents affected (same ISP) | ISP-specific issue |
| ONE agent affected | Agent-side issue (local network) |

### By Layer
| Pattern | Conclusion |
|---|---|
| 0% availability + normal TTFB present | HTTP error responses (auth, 5xx) — application layer |
| 0% availability + no TTFB | Connection failure — network or DNS layer |
| High TTFB + normal network latency | Server-side slowness — application layer |
| High TTFB + high network latency | Network-layer issue — check path visualization |
| High loss + high latency | Congestion or path issue — check specific hop |

### By Correlation
| Pattern | Conclusion |
|---|---|
| Multiple tests, same target | Target is down |
| Multiple tests, same agents failing | Agent location/network issue |
| Multiple tests, same provider in path | Provider outage |
| Multiple tests, same time window | Correlated event (maintenance, attack) |
| Multiple tests, same DNS server | DNS infrastructure issue |

---

## Configuration

### Required: `.env` file

```bash
cp .env.example .env
```

```env
# ThousandEyes API token (required)
# Generate at: ThousandEyes > Account Settings > Users and Roles > User API Tokens
THOUSANDEYES_API_TOKEN=your_token_here

# LLM endpoint (OpenAI-compatible API)
LLM_BASE_URL=https://litellm.prod.outshift.ai/v1
LLM_API_KEY=your_key_here
LLM_MODEL=bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0
```

### Using a Local LLM

Any OpenAI-compatible endpoint works. Examples:

```env
# Ollama
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=not-needed
LLM_MODEL=llama3.1

# vLLM
LLM_BASE_URL=http://localhost:8000/v1
LLM_API_KEY=not-needed
LLM_MODEL=meta-llama/Llama-3.1-8B-Instruct

# LM Studio
LLM_BASE_URL=http://localhost:1234/v1
LLM_API_KEY=not-needed
LLM_MODEL=local-model
```

---

## Usage

### Interactive Mode (recommended)

```bash
./run.sh local
# or
./run.sh docker
```

The agent will:
1. Connect to ThousandEyes MCP server
2. Run environment discovery
3. Drop you into an interactive prompt

Example queries:
```
You: Investigate all active issues
You: What's happening with my Salesforce tests?
You: Check for any BGP anomalies in the last 6 hours
You: Are there any outages affecting my tests?
```

### Single Query Mode

```bash
# Investigate and exit
python3 agent.py -q "Investigate all active issues"

# Skip discovery for faster execution
python3 agent.py --skip-discover -q "Investigate all active issues"
```

### Docker

```bash
# Interactive
docker compose run --rm rca-agent

# Single query
docker compose run --rm rca-agent python3 agent.py -q "Investigate all active issues"
```

---

## Adding Custom Playbooks

Edit `playbooks.yaml` to add new investigation patterns. Each playbook is a list of steps:

```yaml
playbooks:
  my_custom_playbook:
    description: "What this playbook investigates"
    inputs: [alert_id, test_id]    # Variables needed from the router
    steps:
      # Step that calls an MCP tool
      - id: step_name
        tool: mcp_tool_name        # e.g. get_alert, list_events, get_network_app_synthetics_metrics
        params:
          param_name: "{{variable}}"  # Variables filled at runtime
        extract: |
          Tell the LLM exactly what facts to pull from the raw response.
          Be specific. Max N lines.

      # Synthesis step (no MCP call, just LLM reasoning)
      - id: root_cause
        tool: none
        prompt: |
          Given all findings, determine root cause.
          Include decision rules so the LLM follows logic, not intuition.
```

### Available MCP Tools

| Tool | What It Returns |
|---|---|
| `list_tests` | All configured tests |
| `get_network_app_synthetics_test` | Detailed test configuration |
| `list_events` | Network/application events in a time range |
| `get_event_details` | Deep dive into a specific event |
| `list_alerts` | Triggered or cleared alerts |
| `get_alert` | Comprehensive alert details |
| `search_outages` | Network and application outages |
| `get_network_app_synthetics_metrics` | Time-series metrics (availability, TTFB, loss, latency, etc.) |
| `get_anomalies` | Metric anomalies detected over time |
| `get_path_visualization` | Hop-by-hop network path from specific agents |
| `get_full_path_visualization` | Paths from all agents |
| `get_bgp_test_results` | BGP reachability and routing |
| `get_bgp_route_details` | AS path and prefix details |
| `list_endpoint_agents` | Endpoint agents with filtering |
| `get_endpoint_agent_metrics` | Endpoint network/web/wireless metrics |
| `get_account_groups` | Available account groups |
| `run_instant_test` | On-demand test execution (consumes units) |
| `get_views_explanation` | AI-powered visualization explanation |

---

## Project Structure

```
thousandeyes-rca-agent/
├── agent.py              # Entry point — connect, discover, triage, route, investigate
├── playbook_engine.py    # Executes playbook steps with per-step summarization
├── playbooks.yaml        # All investigation playbooks (the core intelligence)
├── agent-prompt.md       # Reference system prompt (for free-form fallback)
├── .env.example          # Credential template
├── .env                  # Your credentials (git-ignored)
├── Dockerfile            # Container with Python 3.11 + Node.js 20
├── docker-compose.yml    # Container orchestration
├── run.sh                # Entry point script (docker or local mode)
├── requirements.txt      # Python dependencies
└── .gitignore
```

---

## Design Decisions

### Why playbooks instead of free-form agent?

We tried the free-form approach first. The LLM called 10 MCP tools, each returned 20-50K tokens of raw JSON, and the conversation hit 204K tokens — exceeding the 200K context window. Even when it fits, a small LLM can't reason over that much unstructured data.

Playbooks solve this by:
1. Defining the exact investigation sequence (no LLM decision-making on strategy)
2. Compressing each tool response immediately (50K → 5 lines)
3. Keeping the final synthesis context under 4K tokens

### Why per-step summarization?

A ThousandEyes metrics response for a 24-hour window can contain hundreds of data points across multiple agents. The LLM doesn't need all of it — it needs "availability dropped to 0% at 07:38 UTC across all agents." The extraction prompt tells it exactly what to pull.

### Why deterministic fault isolation rules?

"All agents affected + network healthy = target-side issue" is a fact, not something an LLM should reason about. Encoding these rules in the playbook means a 7B model produces the same correct conclusion as a frontier model.

### Why auto-routing from triage?

Users shouldn't need to know which playbook to run. The triage step classifies every active issue, and the router picks the right playbook automatically. This makes the tool usable by anyone, not just network engineers.

---

## Rate Limits

The ThousandEyes MCP server counts against your API rate limit:
- OAuth Bearer Token: 240 requests/minute (shared with other integrations)
- OAuth 2.0: 240 requests/minute per client (separate limit)

Each playbook step makes 1 API call. A full investigation (triage + http_error) makes ~14 calls.

## Notes

- Instant Tests consume ThousandEyes units — the agent only runs them if explicitly asked.
- Queries are scoped to your default account group unless you specify otherwise.
- The `discover` playbook runs once per session. Use `--skip-discover` to skip it.
