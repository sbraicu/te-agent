"""Playbook engine — executes investigation playbooks step-by-step with per-step summarization.

Each MCP tool response is immediately compressed to a few lines so even a
small local LLM can run deep investigations without context overflow.

Supports conditional steps: a step with a "condition" field is evaluated
against previous findings by the LLM and skipped if not met.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import yaml
from rich.console import Console

console = Console()


def load_playbooks(path: str = None) -> dict:
    path = path or os.path.join(os.path.dirname(__file__), "playbooks.yaml")
    with open(path) as f:
        return yaml.safe_load(f)["playbooks"]


def resolve_params(params: dict, variables: dict) -> dict:
    """Replace {{var}} placeholders in step params."""
    resolved = {}
    for k, v in params.items():
        if isinstance(v, str):
            for vn, vv in variables.items():
                v = v.replace(f"{{{{{vn}}}}}", str(vv))
            resolved[k] = v
        elif isinstance(v, list):
            new_list = []
            for item in v:
                if isinstance(item, str):
                    for vn, vv in variables.items():
                        item = item.replace(f"{{{{{vn}}}}}", str(vv))
                new_list.append(item)
            resolved[k] = new_list
        else:
            resolved[k] = v
    return resolved


def build_time_window(hours_back: int = 24) -> dict:
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours_back)
    return {
        "window_start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_end": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": f"{hours_back}h",
    }


async def summarize_with_llm(llm_client, model: str, raw_text: str, extract_prompt: str) -> str:
    """Compress a raw MCP response into a few lines using the LLM."""
    resp = llm_client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a data extraction assistant. Extract ONLY the requested "
                    "information from the raw API data. Be concise, factual, no speculation. "
                    "If the data doesn't contain what's asked, say so."
                ),
            },
            {
                "role": "user",
                "content": f"## Instructions\n{extract_prompt}\n\n## Raw Data\n{raw_text[:15000]}",
            },
        ],
        temperature=0,
        max_tokens=500,
    )
    return resp.choices[0].message.content


async def synthesize(llm_client, model: str, findings: dict, prompt: str) -> str:
    """Run a synthesis/reasoning step over accumulated findings."""
    findings_text = "\n\n".join(
        f"### {sid}\n{summary}" for sid, summary in findings.items()
    )
    resp = llm_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"## Investigation Findings\n\n{findings_text}"},
        ],
        temperature=0.2,
        max_tokens=2000,
    )
    return resp.choices[0].message.content


async def evaluate_condition(llm_client, model: str, condition: str, findings: dict) -> bool:
    """Ask the LLM whether a condition is met based on current findings."""
    findings_text = "\n".join(f"- {sid}: {s[:200]}" for sid, s in findings.items())
    resp = llm_client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "Answer YES or NO only. No explanation.",
            },
            {
                "role": "user",
                "content": f"Based on these findings:\n{findings_text}\n\nIs this true: {condition}",
            },
        ],
        temperature=0,
        max_tokens=5,
    )
    return resp.choices[0].message.content.strip().upper().startswith("Y")


async def execute_playbook(
    playbook_name: str,
    mcp_session,
    llm_client,
    model: str,
    variables: Optional[dict] = None,
    playbooks_path: str = None,
) -> tuple[str, dict]:
    """Execute a playbook. Returns (final_result, all_findings)."""
    playbooks = load_playbooks(playbooks_path)
    if playbook_name not in playbooks:
        available = ", ".join(playbooks.keys())
        return f"Unknown playbook '{playbook_name}'. Available: {available}", {}

    pb = playbooks[playbook_name]
    steps = pb["steps"]
    vars_ = {**build_time_window(), **(variables or {})}
    findings = {}

    console.print(f"\n[bold yellow]📋 {playbook_name}: {pb['description']}[/bold yellow]")
    console.print(f"[dim]{len(steps)} steps[/dim]\n")

    for i, step in enumerate(steps, 1):
        step_id = step["id"]
        tool = step.get("tool", "none")

        # Check condition — skip step if not met
        condition = step.get("condition")
        if condition and findings:
            should_run = await evaluate_condition(llm_client, model, condition, findings)
            if not should_run:
                console.print(f"  [{i}/{len(steps)}] [dim]⏭ {step_id} (skipped: {condition})[/dim]")
                findings[step_id] = f"Skipped — condition not met: {condition}"
                continue

        if tool == "none":
            prompt = step.get("prompt", "Summarize all findings.")
            console.print(f"  [{i}/{len(steps)}] [bold magenta]🧠 {step_id}[/bold magenta]")
            result = await synthesize(llm_client, model, findings, prompt)
            findings[step_id] = result
        else:
            params = resolve_params(step.get("params", {}), vars_)
            extract_prompt = step.get("extract", "Summarize key findings. Max 5 lines.")
            console.print(f"  [{i}/{len(steps)}] [cyan]→ {tool}[/cyan]({json.dumps(params)[:120]})")

            try:
                result = await mcp_session.call_tool(tool, params)
                raw = result.content[0].text if result.content else "No data returned"
            except Exception as e:
                raw = f"Tool call failed: {e}"
                console.print(f"    [red]⚠ {raw[:100]}[/red]")

            summary = await summarize_with_llm(llm_client, model, raw, extract_prompt)
            findings[step_id] = summary
            console.print(f"    [green]✓[/green] {summary[:100]}...")

    final = findings.get(steps[-1]["id"], "No result.")
    return final, findings


# ── Decision tree: triage output → next playbooks ──

ROUTING_PROMPT = """You are a classification engine. Given triage results, output a JSON array
of playbooks to run next. Each entry has "name" and "variables".

RULES:
- For each active alert/event, pick the best playbook:
  - HTTP errors, 4xx/5xx, timeouts on http-server tests → http_error
  - DNS resolution failures on dns-server/dns-trace tests → dns_failure
  - SSL/TLS/certificate errors → ssl_tls
  - 100% packet loss or target unreachable → connectivity
  - Elevated latency or response time → latency
  - BGP reachability drops or route changes → bgp
  - Endpoint agent degradation → endpoint
- If 3+ tests failing simultaneously → add multi_test with all test_ids
- Always include alert_id and test_id in variables when available
- If no issues found, return empty array

Respond ONLY with JSON, no markdown:
{"playbooks": [{"name": "...", "variables": {"alert_id": "...", "test_id": "..."}}]}"""


async def route_from_triage(llm_client, model: str, triage_findings: dict) -> list[dict]:
    """Use triage findings to decide which investigation playbooks to run."""
    findings_text = "\n\n".join(
        f"### {sid}\n{s}" for sid, s in triage_findings.items()
    )
    resp = llm_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": ROUTING_PROMPT},
            {"role": "user", "content": findings_text},
        ],
        temperature=0,
        max_tokens=500,
    )
    text = resp.choices[0].message.content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)["playbooks"]
