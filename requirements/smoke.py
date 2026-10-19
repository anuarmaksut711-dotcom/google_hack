"""Credential-free import smoke: run with the installed component interpreter."""

import argparse
from importlib import metadata
import json
import sys
from zoneinfo import ZoneInfo


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("component", choices=("core", "agent", "mcp", "web"))
    args = parser.parse_args()
    import campuspulse_recovery

    # The shared namespace must not eagerly import any SDK/server stack.
    assert not any(name in sys.modules for name in ("google.adk", "vertexai", "mcp"))
    assert campuspulse_recovery.__version__ == metadata.version("campuspulse-recovery")
    assert ZoneInfo("Asia/Qyzylorda").key == "Asia/Qyzylorda"
    for forbidden in ("fastmcp", "workspace-mcp"):
        try:
            metadata.version(forbidden)
        except metadata.PackageNotFoundError:
            pass
        else:
            raise AssertionError(f"Unexpected distribution: {forbidden}")

    versions = {"python": sys.version.split()[0], "component": args.component}
    if args.component in ("agent", "mcp"):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.server.fastmcp import FastMCP

        assert metadata.version("mcp") == "1.30.0"
        server = FastMCP("import_smoke", stateless_http=True, json_response=True)
        assert server.streamable_http_app() is not None
        assert callable(ClientSession) and callable(streamablehttp_client)
        versions["mcp"] = metadata.version("mcp")
    if args.component == "agent":
        from google.adk.agents import Agent
        from google.adk.tools import FunctionTool
        from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
        from google.genai import types

        assert Agent(name="import_smoke", model="gemini-3.5-flash").name == "import_smoke"
        assert all(callable(item) for item in (FunctionTool, McpToolset, StreamableHTTPConnectionParams))
        assert types.Content(role="user").role == "user"
        versions["google-adk"] = metadata.version("google-adk")
    if args.component in ("agent", "web"):
        import vertexai
        from vertexai import agent_engines, types

        assert callable(vertexai.Client) and callable(agent_engines.AdkApp)
        assert types.IdentityType.SERVICE_ACCOUNT is not None
        versions["google-cloud-aiplatform"] = metadata.version("google-cloud-aiplatform")
    if args.component in ("mcp", "web"):
        from google.cloud.firestore import AsyncClient, Client

        assert callable(Client) and callable(AsyncClient)
        versions["google-cloud-firestore"] = metadata.version("google-cloud-firestore")
    if args.component == "mcp":
        from googleapiclient.discovery import build
        from google.auth.transport.requests import Request
        from google.oauth2.id_token import fetch_id_token, verify_oauth2_token

        assert all(callable(item) for item in (build, Request, fetch_id_token, verify_oauth2_token))
    print(json.dumps(versions, sort_keys=True))


if __name__ == "__main__":
    main()
