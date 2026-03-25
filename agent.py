"""ThousandEyes RCA Agent — playbook-driven, works with any LLM.

Flow:
  1. Connect to ThousandEyes MCP server
  2. Discover environment (once per session)
  3. On each query: triage → auto-route to investigation playbooks → RCA
"""

import argparse
import asyncio
import json
import os
import sys
from typing import Optional

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from openai import OpenAI
from rich.console import Console
from rich.markdown import Markdown

from playbook_engine import execute_playbook, route_from_triage

load_dotenv()
console = Console()


class RCAAgent:
    def __init__(self):
        self.session: Optional[ClientSession] = None
        self.llm = OpenAI(
            base_url=os.getenv("LLM_BASE_URL", "https://litellm.prod.outshift.ai/v1"),
            api_key=os.getenv("LLM_API_KEY", "not-needed"),
        )
        self.model = os.getenv(
            "LLM_MODEL", "bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0"
        )
        self._streams_ctx = None
        self._session_ctx = None
        self.env_context = None  # populated by discover playbook

    async def connect(self, server_url: str, token: str):
        headers = {"Authorization": f"Bearer {token}"}
        self._streams_ctx = streamablehttp_client(url=server_url, headers=headers)
        read_stream, write_stream, _ = await self._streams_ctx.__aenter__()
        self._session_ctx = ClientSession(read_stream, write_stream)
        self.session = await self._session_ctx.__aenter__()
        await self.session.initialize()
        tools_resp = await self.session.list_tools()
        console.print(f"[green]Connected — {len(tools_resp.tools)} tools available[/green]")

    async def discover(self):
        """Run discovery playbook once to learn the environment."""
        console.print("\n[bold]Phase 0: Discovering environment...[/bold]")
        result, findings = await execute_playbook(
            "discover", self.session, self.llm, self.model
        )
        self.env_context = result
        console.print(Markdown(result))
        console.print()

    async def investigate(self, query: str) -> str:
        """Full investigation: triage → route → run playbooks."""
        all_results = []

        # Phase 1: Triage
        console.print(f"\n[bold]Phase 1: Triage[/bold]")
        triage_result, triage_findings = await execute_playbook(
            "triage", self.session, self.llm, self.model,
            variables={"window": "24h"},
        )
        all_results.append(triage_result)

        # Phase 2: Route — decide which playbooks to run based on triage
        console.print(f"\n[bold]Phase 2: Routing to investigation playbooks...[/bold]")
        try:
            next_playbooks = await route_from_triage(self.llm, self.model, triage_findings)
        except Exception as e:
            console.print(f"[red]Routing failed: {e}. Showing triage results only.[/red]")
            return triage_result

        if not next_playbooks:
            console.print("[green]No issues requiring investigation.[/green]")
            return triage_result

        console.print(
            f"[dim]Will run: {', '.join(p['name'] for p in next_playbooks)}[/dim]"
        )

        # Phase 3: Run each investigation playbook
        for i, plan in enumerate(next_playbooks, 1):
            console.print(f"\n[bold]Phase 3.{i}: Investigating ({plan['name']})[/bold]")
            result, _ = await execute_playbook(
                plan["name"], self.session, self.llm, self.model,
                variables=plan.get("variables", {}),
            )
            all_results.append(result)

        return "\n\n---\n\n".join(all_results)

    async def chat_loop(self):
        console.print("\n[bold]ThousandEyes RCA Agent[/bold]")
        console.print("Type a question or 'investigate' to run full analysis. 'quit' to exit.\n")

        await self.discover()

        while True:
            try:
                query = input("You: ").strip()
                if not query:
                    continue
                if query.lower() in ("quit", "exit"):
                    break
                result = await self.investigate(query)
                console.print()
                console.print(Markdown(result))
                console.print()
            except KeyboardInterrupt:
                break
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")

    async def cleanup(self):
        if self._session_ctx:
            await self._session_ctx.__aexit__(None, None, None)
        if self._streams_ctx:
            await self._streams_ctx.__aexit__(None, None, None)


async def main():
    parser = argparse.ArgumentParser(description="ThousandEyes RCA Agent")
    parser.add_argument("--query", "-q", help="Single query mode")
    parser.add_argument("--server-url", default="https://api.thousandeyes.com/mcp")
    parser.add_argument("--skip-discover", action="store_true", help="Skip environment discovery")
    args = parser.parse_args()

    token = os.getenv("THOUSANDEYES_API_TOKEN")
    if not token:
        console.print("[red]Error: THOUSANDEYES_API_TOKEN not set in .env[/red]")
        sys.exit(1)

    agent = RCAAgent()
    try:
        await agent.connect(args.server_url, token)
        if not args.skip_discover:
            await agent.discover()
        if args.query:
            result = await agent.investigate(args.query)
            console.print(Markdown(result))
        else:
            await agent.chat_loop()
    finally:
        await agent.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
