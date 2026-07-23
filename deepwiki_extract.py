#!/usr/bin/env python3
"""
DeepWiki Documentation Exporter
Exports DeepWiki documentation repositories to local Markdown files.
Supports both single-file aggregated export and structured multi-file directory output.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

# Configuration Defaults
DEFAULT_MCP_ENDPOINT = "https://mcp.deepwiki.com/mcp"
DEFAULT_TIMEOUT_SEC = 30
MAX_RETRIES = 3
BACKOFF_FACTOR = 1.5


class DeepWikiURLParser:
    """Parses and normalizes target repository identifiers from various inputs."""

    @staticmethod
    def parse_repo_name(input_str: str) -> str:
        """
        Extracts owner/repo identifier from URLs or direct repo string inputs.
        
        Examples:
            - https://deepwiki.com/d3bvstack/Inception -> d3bvstack/Inception
            - https://deepwiki.com/d3bvstack/Inception/1-overview -> d3bvstack/Inception
            - https://github.com/d3bvstack/Inception -> d3bvstack/Inception
            - d3bvstack/Inception -> d3bvstack/Inception
        """
        input_str = input_str.strip()
        if input_str.startswith("http://") or input_str.startswith("https://"):
            parsed = urllib.parse.urlparse(input_str)
            path_parts = [p for p in parsed.path.strip("/").split("/") if p]
            if len(path_parts) >= 2:
                return f"{path_parts[0]}/{path_parts[1]}"
            raise ValueError(f"Unable to extract owner/repo from URL path: '{input_str}'")
        
        parts = [p for p in input_str.strip("/").split("/") if p]
        if len(parts) == 2:
            return f"{parts[0]}/{parts[1]}"
        
        raise ValueError(f"Invalid repository string format. Expected 'owner/repo', got '{input_str}'")


class DeepWikiMCPClient:
    """Client for communicating with Cognition's DeepWiki MCP Server via JSON-RPC 2.0."""

    def __init__(self, endpoint_url: str = DEFAULT_MCP_ENDPOINT, timeout: int = DEFAULT_TIMEOUT_SEC):
        self.endpoint_url = endpoint_url
        self.timeout = timeout

    def _execute_jsonrpc_call(self, method: str, params: dict) -> dict:
        """Executes a JSON-RPC 2.0 call over HTTP with exponential backoff retries."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        }
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "DeepWikiExporter/1.0"
        }

        last_exception = None
        for attempt in range(MAX_RETRIES):
            try:
                req = urllib.request.Request(self.endpoint_url, data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    raw_res = response.read().decode("utf-8")
                    return self._parse_rpc_response(raw_res)
            except Exception as e:
                last_exception = e
                if attempt < MAX_RETRIES - 1:
                    sleep_time = BACKOFF_FACTOR ** attempt
                    time.sleep(sleep_time)

        raise RuntimeError(f"MCP RPC call failed after {MAX_RETRIES} attempts. Error: {last_exception}")

    def _parse_rpc_response(self, raw_res: str) -> dict:
        """Parses standard JSON-RPC responses as well as line-delimited SSE chunks."""
        raw_res = raw_res.strip()
        
        try:
            return json.loads(raw_res)
        except json.JSONDecodeError:
            pass

        lines = raw_res.split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith("data:"):
                data_content = line[5:].strip()
                if data_content == "[DONE]":
                    continue
                try:
                    return json.loads(data_content)
                except json.JSONDecodeError:
                    continue

        raise ValueError(f"Failed to decode valid JSON-RPC payload from endpoint response: {raw_res[:200]}...")

    def fetch_wiki_structure(self, repo_name: str) -> Union[dict, str]:
        """Fetches the table of contents structure for a repository."""
        res = self._execute_jsonrpc_call("tools/call", {
            "name": "read_wiki_structure",
            "arguments": {"repoName": repo_name}
        })
        return self._extract_result_content(res)

    def fetch_wiki_contents(self, repo_name: str) -> str:
        """Fetches complete pre-rendered markdown documentation for a repository."""
        res = self._execute_jsonrpc_call("tools/call", {
            "name": "read_wiki_contents",
            "arguments": {"repoName": repo_name}
        })
        content = self._extract_result_content(res)
        if isinstance(content, dict):
            return json.dumps(content, indent=2)
        return str(content)

    def _extract_result_content(self, response: dict) -> Union[str, dict]:
        """Extracts content payload from JSON-RPC tool response."""
        if "error" in response:
            raise RuntimeError(f"RPC Remote Error [{response['error'].get('code')}]: {response['error'].get('message')}")

        result = response.get("result", {})
        if "content" in result and isinstance(result["content"], list):
            texts = []
            for item in result["content"]:
                if item.get("type") == "text":
                    texts.append(item.get("text", ""))
            return "\n".join(texts)
        
        return str(result)


class DeepWikiScraperFallback:
    """Fallback parser for direct HTML extraction if the MCP interface is unreachable."""

    @staticmethod
    def scrape_repo_wiki(repo_name: str, timeout: int = DEFAULT_TIMEOUT_SEC) -> str:
        url = f"https://deepwiki.com/{repo_name}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            html = response.read().decode("utf-8")

        # Extract from Next.js __NEXT_DATA__ block if available
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
            page_props = data.get("props", {}).get("pageProps", {})
            if "content" in page_props:
                return page_props["content"]
            if "wikiData" in page_props:
                return json.dumps(page_props["wikiData"], indent=2)

        # Fallback to HTML text stripping
        body = re.sub(r'<script.*?>.*?</script>', '', html, flags=re.DOTALL)
        body = re.sub(r'<style.*?>.*?</style>', '', body, flags=re.DOTALL)
        clean_text = re.sub(r'<[^>]+>', '', body)
        return clean_text


class MarkdownWikiProcessor:
    """Processes and formats Markdown content into pages or sections."""

    @classmethod
    def split_into_pages(cls, full_markdown: str) -> List[Tuple[str, str]]:
        """Splits unified markdown document into discrete section pages based on headers."""
        lines = full_markdown.splitlines()
        pages: List[Tuple[str, str]] = []
        current_title = "0-Overview"
        current_lines: List[str] = []

        i = 0
        while i < len(lines):
            line = lines[i]
            page_tag_match = re.match(r'^#\s*Page:\s*(.+)$', line, re.IGNORECASE)
            header_match = re.match(r'^(#+)\s+(.+)$', line)
            
            if page_tag_match:
                # Found a # Page: marker - this is the primary page delimiter
                if current_lines:
                    pages.append((current_title, "\n".join(current_lines)))
                    current_lines = []
                current_title = page_tag_match.group(1).strip()
                # Skip the # Page: line itself (don't include in content)
                i += 1
                # Skip any blank lines
                while i < len(lines) and not lines[i].strip():
                    i += 1
                # Check if next non-blank line is a duplicate heading with same title
                if i < len(lines):
                    next_header = re.match(r'^#\s+(.+)$', lines[i])
                    if next_header and next_header.group(1).strip() == current_title:
                        # Include the duplicate heading as the page's H1
                        current_lines.append(lines[i])
                        i += 1
                continue
            elif header_match and header_match.group(1) == "#":
                # Regular H1 heading - acts as a page break
                if current_lines:
                    pages.append((current_title, "\n".join(current_lines)))
                    current_lines = []
                current_title = header_match.group(2).strip()
            
            current_lines.append(line)
            i += 1

        if current_lines:
            pages.append((current_title, "\n".join(current_lines)))

        return pages if pages else [("1-Overview", full_markdown)]

    @classmethod
    def slugify(cls, text: str) -> str:
        """Converts title string into a filesystem-safe slug."""
        text = text.lower()
        text = re.sub(r'[^\w\s-]', '', text)
        return re.sub(r'[-\s]+', '-', text).strip('-')


def export_deepwiki(
    repo_input: str,
    output_path: str,
    multi_file: bool = False,
    mcp_endpoint: str = DEFAULT_MCP_ENDPOINT
):
    """Main execution function to fetch and save DeepWiki docs."""
    repo_name = DeepWikiURLParser.parse_repo_name(repo_input)
    print(f"[*] Target Repository: {repo_name}")
    print(f"[*] Connecting to DeepWiki MCP service ({mcp_endpoint})...")

    raw_markdown = ""
    try:
        client = DeepWikiMCPClient(endpoint_url=mcp_endpoint)
        raw_markdown = client.fetch_wiki_contents(repo_name)
        print(f"[+] Successfully fetched documentation via MCP.")
    except Exception as e:
        print(f"[!] MCP fetch failed ({e}). Falling back to Web Scraper...")
        try:
            raw_markdown = DeepWikiScraperFallback.scrape_repo_wiki(repo_name)
            print(f"[+] Successfully fetched documentation via Web Scraper fallback.")
        except Exception as fallback_err:
            print(f"[X] Error: Failed to retrieve documentation from all providers ({fallback_err}).")
            sys.exit(1)

    if not raw_markdown.strip():
        print(f"[X] Error: Fetched documentation content is empty.")
        sys.exit(1)

    header_block = (
        f"<!--\n"
        f"  Generated by DeepWiki Exporter\n"
        f"  Repository: {repo_name}\n"
        f"  Source: https://deepwiki.com/{repo_name}\n"
        f"-->\n\n"
    )

    if multi_file:
        out_dir = Path(output_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        pages = MarkdownWikiProcessor.split_into_pages(raw_markdown)
        
        index_entries = [f"# {repo_name} Documentation\n\n## Table of Contents\n"]
        
        for idx, (title, content) in enumerate(pages, 1):
            slug = MarkdownWikiProcessor.slugify(title) or f"page-{idx}"
            file_name = f"{idx:02d}-{slug}.md"
            file_path = out_dir / file_name
            
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(header_block + content)
            
            index_entries.append(f"{idx}. [{title}]({file_name})")
            print(f"  [->] Saved: {file_path}")

        index_path = out_dir / "README.md"
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(header_block + "\n".join(index_entries) + "\n")
        print(f"[+] Multi-file Wiki bundle written to: {out_dir.resolve()}")

    else:
        if os.path.isdir(output_path):
            output_file = os.path.join(output_path, f"{repo_name.replace('/', '_')}_wiki.md")
        else:
            output_file = output_path if output_path.endswith(".md") else f"{output_path}.md"

        parent_dir = os.path.dirname(os.path.abspath(output_file))
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        
        final_content = header_block + f"# {repo_name} - DeepWiki Documentation\n\n" + raw_markdown
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(final_content)
        
        print(f"[+] Complete Wiki Markdown exported to: {os.path.abspath(output_file)}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate local Markdown export of any DeepWiki GitHub repository."
    )
    parser.add_argument(
        "repo",
        help="Repository input (e.g. 'https://deepwiki.com/d3bvstack/Inception' or 'd3bvstack/Inception')"
    )
    parser.add_argument(
        "-o", "--output",
        default=".",
        help="Output target file path or directory (default: current directory)"
    )
    parser.add_argument(
        "--multi-file",
        action="store_true",
        help="Split wiki sections into individual markdown files inside a directory"
    )
    parser.add_argument(
        "--mcp-endpoint",
        default=DEFAULT_MCP_ENDPOINT,
        help=f"Custom MCP server URL (default: {DEFAULT_MCP_ENDPOINT})"
    )

    args = parser.parse_args()
    
    # Fix: Access flag as `args.multi_file` instead of `args.multi-file`
    export_deepwiki(
        repo_input=args.repo,
        output_path=args.output,
        multi_file=args.multi_file,
        mcp_endpoint=args.mcp_endpoint
    )


if __name__ == "__main__":
    main()
