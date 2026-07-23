# DeepWiki Documentation Exporter

A zero-dependency Python tool for exporting documentation repositories from [DeepWiki](https://deepwiki.com) to clean, local Markdown. Supports high-speed JSON-RPC fetching via DeepWiki's Model Context Protocol (MCP) server, automatic web-scraping fallback, and multi-file document bundling.

## Features

- **Flexible Target Parsing**: Accepts direct repo strings (`owner/repo`), DeepWiki page URLs, and standard GitHub repository links.
- **MCP Native Client**: Interfaces directly with DeepWiki's MCP endpoint (`https://mcp.deepwiki.com/mcp`) using JSON-RPC 2.0 and Server-Sent Events (SSE) streaming support with automatic exponential backoff retries.
- **Resilient Fallback**: Automatically falls back to client-side extraction (`__NEXT_DATA__` standard page JSON / direct DOM) if the primary MCP server is unreachable.
- **Single or Multi-File Output**:
  - **Single File**: Aggregates all chapters into a unified Markdown file.
  - **Multi-File (`--multi-file`)**: Splits content into organized, sequentially numbered Markdown files with an auto-generated table of contents (`README.md`).
- **Zero External Dependencies**: Built entirely on standard Python 3 standard library packages (`urllib`, `json`, `re`, `pathlib`, `argparse`).

## Installation

Ensure you have Python 3.8+ installed. Save the script as `export_deepwiki.py` and grant execution permissions:

```bash
chmod +x export_deepwiki.py
```

No `pip install` commands are required.

## Usage

### Basic Single-File Export

Exports the full documentation into a single aggregated Markdown file in the current working directory:

```bash
./export_deepwiki.py d3bvstack/Inception
```

Or pass a full DeepWiki or GitHub URL:

```bash
./export_deepwiki.py https://deepwiki.com/d3bvstack/Inception
```

### Multi-File Directory Export

Split sections into structured, ordered files inside a target output directory:

```bash
./export_deepwiki.py d3bvstack/Inception -o ./docs/inception --multi-file
```

Generates the following structure:
```text
docs/inception/
├── README.md              # Auto-generated Index & TOC
├── 01-0-overview.md       # Chapter content
├── 02-architecture.md
└── ...
```

### Custom Output File

Specify an explicit Markdown file destination:

```bash
./export_deepwiki.py d3bvstack/Inception -o ./Inception_Wiki.md
```

### Custom MCP Server Endpoint

If running a local or private MCP gateway:

```bash
./export_deepwiki.py d3bvstack/Inception --mcp-endpoint https://your-mcp-proxy.local/mcp
```

## CLI Reference

```text
positional arguments:
  repo                  Repository identifier or URL. Supported forms:
                          - 'owner/repo'
                          - 'https://deepwiki.com/owner/repo'
                          - 'https://deepwiki.com/owner/repo/1-overview'
                          - 'https://github.com/owner/repo'

options:
  -h, --help            Show this help message and exit
  -o, --output OUTPUT   Target output file path or directory (default: current directory)
  --multi-file          Split wiki sections into individual markdown files
  --mcp-endpoint URL    Custom MCP JSON-RPC endpoint URL
                        (default: https://mcp.deepwiki.com/mcp)
```

## How It Works

1. **Target Normalization**: Extracts the fundamental `owner/repo` path using standard URL scheme parsing.
2. **MCP Direct Extraction**: Sends a `tools/call` JSON-RPC method request to call `read_wiki_contents` over HTTP POST. Supports chunked SSE parsing.
3. **Scraper Fallback**: If the MCP gateway fails, the client issues a standard HTTP GET request to DeepWiki to extract structured JSON props from the Next.js standard data stream (`__NEXT_DATA__`).
4. **AST Processing**: Parses logical `# Page:` or `H1` level boundaries to construct structured directory hierarchies or flattened unified Markdown documents.
