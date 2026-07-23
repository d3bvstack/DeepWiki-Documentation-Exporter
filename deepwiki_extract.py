#!/usr/bin/env python3
"""
DeepWiki Documentation Exporter
Exports DeepWiki documentation repositories to local Markdown files.
Automatically detects and parses sidebar HTML index components and Next.js page state
from DeepWiki web pages to name files accurately according to their route slugs.
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


class IndexHTMLParser:
    """Parses DeepWiki sidebar HTML elements to extract TOC structure and file slugs."""

    @staticmethod
    def parse_index_html(html_content: str) -> List[Dict[str, Union[str, int]]]:
        """
        Parses <li> elements inside sidebar navigation.
        Returns a list of dicts with keys: 'title', 'slug', 'href', 'padding_px'.
        """
        pattern = re.compile(
            r'<li[^>]*style="[^"]*padding-left:\s*(\d+)px[^"]*"[^>]*>\s*'
            r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>\s*</li>',
            re.DOTALL | re.IGNORECASE
        )

        entries = []
        for match in pattern.finditer(html_content):
            padding_px = int(match.group(1))
            href = match.group(2).strip()
            title = re.sub(r'<[^>]+>', '', match.group(3)).strip()  # Clean nested HTML tags

            # Extract slug from href e.g. /d3bvstack/98-webserv/1.1-getting-started -> 1.1-getting-started
            href_parts = [p for p in href.strip("/").split("/") if p]
            slug = href_parts[-1] if href_parts else ""

            entries.append({
                "title": title,
                "slug": slug,
                "href": href,
                "padding_px": padding_px
            })

        return entries


class DeepWikiSidebarFetcher:
    """Automatically fetches and extracts sidebar index components directly from DeepWiki."""

    @staticmethod
    def fetch_index(
        repo_name: str,
        client: Optional["DeepWikiMCPClient"] = None,
        timeout: int = DEFAULT_TIMEOUT_SEC
    ) -> List[Dict[str, Union[str, int]]]:
        """Fetches page HTML and extracts route slugs and titles for multi-file exports."""
        url = f"https://deepwiki.com/{repo_name}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }

        entries = []
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                html = response.read().decode("utf-8")

            repo_parts = repo_name.split("/")
            repo_slug = repo_parts[-1] if repo_parts else repo_name

            # Method 1: Extract <a> links matching /owner/repo/slug pattern
            link_pattern = re.compile(
                r'href=["\'](?:https?://deepwiki\.com)?/(?:' + re.escape(repo_name) + r'|' + re.escape(repo_slug) + r')/([^"\']+)["\'][^>]*>(.*?)</a>',
                re.DOTALL | re.IGNORECASE
            )

            seen_slugs = set()
            for match in link_pattern.finditer(html):
                slug = match.group(1).strip("/")
                raw_title = match.group(2)
                title = re.sub(r'<[^>]+>', '', raw_title).strip()

                padding_px = 0
                li_before = html[max(0, match.start() - 250):match.start()]
                pad_match = re.search(r'padding-left:\s*(\d+)px', li_before)
                if pad_match:
                    padding_px = int(pad_match.group(1))

                if slug and slug not in seen_slugs:
                    seen_slugs.add(slug)
                    entries.append({
                        "title": title,
                        "slug": slug,
                        "padding_px": padding_px
                    })

            if entries:
                return entries

            # Method 2: Extract Next.js __NEXT_DATA__ JSON block
            match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
            if match:
                next_data = json.loads(match.group(1))
                json_str = json.dumps(next_data)
                slug_matches = re.findall(r'/(?:' + re.escape(repo_name) + r')/([0-9][a-zA-Z0-9.-]*)', json_str)
                for s in slug_matches:
                    if s not in seen_slugs:
                        seen_slugs.add(s)
                        clean_title = re.sub(r'^[0-9.]+-?', '', s).replace('-', ' ').title()
                        entries.append({"title": clean_title, "slug": s, "padding_px": 0})
                if entries:
                    return entries

        except Exception:
            pass

        # Method 3: Try fetching wiki structure from MCP service if available
        if client:
            try:
                struct_res = client.fetch_wiki_structure(repo_name)
                if isinstance(struct_res, str):
                    try:
                        struct_data = json.loads(struct_res)
                    except Exception:
                        struct_data = None
                else:
                    struct_data = struct_res

                if isinstance(struct_data, list):
                    for item in struct_data:
                        if isinstance(item, dict):
                            s = item.get("id") or item.get("slug") or item.get("path")
                            t = item.get("title") or s
                            if s:
                                entries.append({"title": t, "slug": s, "padding_px": 0})
            except Exception:
                pass

        return entries


class DeepWikiURLParser:
    """Parses and normalizes target repository identifiers from various inputs."""

    @staticmethod
    def parse_repo_name(input_str: str) -> str:
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
                    time.sleep(BACKOFF_FACTOR ** attempt)

        raise RuntimeError(f"MCP RPC call failed after {MAX_RETRIES} attempts. Error: {last_exception}")

    def _parse_rpc_response(self, raw_res: str) -> dict:
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

        raise ValueError(f"Failed to decode valid JSON-RPC payload from endpoint response.")

    def fetch_wiki_structure(self, repo_name: str) -> Union[dict, str]:
        res = self._execute_jsonrpc_call("tools/call", {
            "name": "read_wiki_structure",
            "arguments": {"repoName": repo_name}
        })
        return self._extract_result_content(res)

    def fetch_wiki_contents(self, repo_name: str) -> str:
        res = self._execute_jsonrpc_call("tools/call", {
            "name": "read_wiki_contents",
            "arguments": {"repoName": repo_name}
        })
        content = self._extract_result_content(res)
        if isinstance(content, dict):
            return json.dumps(content, indent=2)
        return str(content)

    def _extract_result_content(self, response: dict) -> Union[str, dict]:
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
    def scrape_repo_wiki(repo_name: str, timeout: int = DEFAULT_TIMEOUT_SEC) -> Tuple[str, str]:
        url = f"https://deepwiki.com/{repo_name}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            html = response.read().decode("utf-8")

        index_matches = re.findall(r'<li[^>]*style="padding-left:[^"]*"[^>]*>.*?</li>', html, re.DOTALL)
        index_html = "".join(index_matches) if index_matches else ""

        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
            page_props = data.get("props", {}).get("pageProps", {})
            if "content" in page_props:
                return page_props["content"], index_html
            if "wikiData" in page_props:
                return json.dumps(page_props["wikiData"], indent=2), index_html

        body = re.sub(r'<script.*?>.*?</script>', '', html, flags=re.DOTALL)
        body = re.sub(r'<style.*?>.*?</style>', '', body, flags=re.DOTALL)
        clean_text = re.sub(r'<[^>]+>', '', body)
        return clean_text, index_html


class MarkdownWikiProcessor:
    """Processes and formats Markdown content into pages or sections."""

    @classmethod
    def slugify(cls, text: str) -> str:
        """Converts title string into a filesystem-safe slug."""
        text = text.lower()
        text = re.sub(r'[^\w\s-]', '', text)
        return re.sub(r'[-\s]+', '-', text).strip('-')

    @classmethod
    def split_into_pages(
        cls,
        full_markdown: str,
        index_entries: Optional[List[Dict[str, Union[str, int]]]] = None
    ) -> List[Tuple[str, str, str]]:
        """
        Splits unified markdown document into discrete section pages based on index or headers.
        Returns list of tuples: (page_title, file_slug, page_content)
        """
        lines = full_markdown.splitlines()
        pages: List[Tuple[str, List[str]]] = []
        current_title = "Overview"
        current_lines: List[str] = []

        i = 0
        while i < len(lines):
            line = lines[i]
            page_tag_match = re.match(r'^#\s*Page:\s*(.+)$', line, re.IGNORECASE)
            header_match = re.match(r'^(#+)\s+(.+)$', line)
            
            if page_tag_match:
                if current_lines:
                    pages.append((current_title, current_lines))
                    current_lines = []
                current_title = page_tag_match.group(1).strip()
                i += 1
                while i < len(lines) and not lines[i].strip():
                    i += 1
                if i < len(lines):
                    next_header = re.match(r'^#\s+(.+)$', lines[i])
                    if next_header and next_header.group(1).strip() == current_title:
                        current_lines.append(lines[i])
                        i += 1
                continue
            elif header_match and header_match.group(1) == "#":
                if current_lines:
                    pages.append((current_title, current_lines))
                    current_lines = []
                current_title = header_match.group(2).strip()
            
            current_lines.append(line)
            i += 1

        if current_lines:
            pages.append((current_title, current_lines))

        # Build lookup tables from index entries
        slug_map = {}
        title_map = {}
        if index_entries:
            for entry in index_entries:
                if entry.get("title") and entry.get("slug"):
                    slug_map[entry["title"].lower()] = entry["slug"]
                    title_map[cls.slugify(entry["title"])] = entry["slug"]

        processed_pages: List[Tuple[str, str, str]] = []
        for idx, (title, content_lines) in enumerate(pages, 1):
            clean_title = title.strip()
            norm_title = clean_title.lower()
            slugified_title = cls.slugify(clean_title)

            # Match slug from index component automatically
            if index_entries and norm_title in slug_map:
                slug = slug_map[norm_title]
            elif index_entries and slugified_title in title_map:
                slug = title_map[slugified_title]
            elif index_entries and idx - 1 < len(index_entries):
                slug = index_entries[idx - 1]["slug"]
            else:
                slug = slugified_title or f"page-{idx}"

            processed_pages.append((clean_title, slug, "\n".join(content_lines)))

        return processed_pages


def export_deepwiki(
    repo_input: str,
    output_path: str,
    multi_file: bool = False,
    mcp_endpoint: str = DEFAULT_MCP_ENDPOINT
):
    """Main execution function to fetch and save DeepWiki docs."""
    repo_name = DeepWikiURLParser.parse_repo_name(repo_input)
    print(f"[*] Target Repository: {repo_name}")

    client = None
    try:
        client = DeepWikiMCPClient(endpoint_url=mcp_endpoint)
    except Exception:
        pass

    # Fetch and parse sidebar index component
    print(f"[*] Extracting sidebar index component from DeepWiki...")
    index_entries = DeepWikiSidebarFetcher.fetch_index(repo_name, client=client)
    if index_entries:
        print(f"[+] Successfully extracted {len(index_entries)} navigation items from sidebar component.")
    else:
        print(f"[!] Sidebar component not found. Falling back to title slugification.")

    raw_markdown = ""
    print(f"[*] Fetching wiki content via MCP ({mcp_endpoint})...")
    try:
        if not client:
            client = DeepWikiMCPClient(endpoint_url=mcp_endpoint)
        raw_markdown = client.fetch_wiki_contents(repo_name)
        print(f"[+] Successfully fetched documentation content via MCP.")
    except Exception as e:
        print(f"[!] MCP fetch failed ({e}). Falling back to Web Scraper...")
        try:
            raw_markdown, scraped_index = DeepWikiScraperFallback.scrape_repo_wiki(repo_name)
            if not index_entries and scraped_index:
                index_entries = IndexHTMLParser.parse_index_html(scraped_index)
                print(f"[+] Extracted {len(index_entries)} navigation items from web scraper fallback.")
            print(f"[+] Successfully fetched documentation content via Web Scraper fallback.")
        except Exception as fallback_err:
            print(f"[X] Error: Failed to retrieve documentation ({fallback_err}).")
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
        pages = MarkdownWikiProcessor.split_into_pages(raw_markdown, index_entries)
        
        index_entries_md = [f"# {repo_name} Documentation\n\n## Table of Contents\n"]
        
        for idx, (title, slug, content) in enumerate(pages, 1):
            file_name = f"{slug}.md" if not slug.endswith(".md") else slug
            file_path = out_dir / file_name
            
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(header_block + content)
            
            # Preserve hierarchy indentation matching sidebar HTML
            indent_spaces = ""
            if index_entries and idx - 1 < len(index_entries):
                indent_px = index_entries[idx - 1].get("padding_px", 0)
                indent_spaces = "  " * (indent_px // 12)
            
            index_entries_md.append(f"{indent_spaces}- [{title}]({file_name})")
            print(f"  [->] Saved: {file_path}")

        index_path = out_dir / "README.md"
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(header_block + "\n".join(index_entries_md) + "\n")
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
        help="Repository input (e.g. 'd3bvstack/98-webserv' or 'https://deepwiki.com/d3bvstack/98-webserv')"
    )
    parser.add_argument(
        "-o", "--output",
        default=".",
        help="Output target file path or directory (default: current directory)"
    )
    parser.add_argument(
        "--multi-file",
        action="store_true",
        help="Split wiki sections into individual markdown files named by sidebar slugs"
    )
    parser.add_argument(
        "--mcp-endpoint",
        default=DEFAULT_MCP_ENDPOINT,
        help=f"Custom MCP server URL (default: {DEFAULT_MCP_ENDPOINT})"
    )

    args = parser.parse_args()

    export_deepwiki(
        repo_input=args.repo,
        output_path=args.output,
        multi_file=args.multi_file,
        mcp_endpoint=args.mcp_endpoint
    )


if __name__ == "__main__":
    main()
