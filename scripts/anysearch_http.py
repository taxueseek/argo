#!/usr/bin/env python3
"""anysearch HTTP API wrapper — JSON-RPC over HTTPS, 零外部依赖."""
import sys, json, os, urllib.request

URL = "https://api.anysearch.com/mcp"
TIMEOUT = 12

def search(query: str, n: int = 5, domain: str = "", sub_domain: str = "") -> dict:
    args = {"query": query, "max_results": n}
    if domain:
        args["domain"] = domain
    if sub_domain:
        args["sub_domain"] = sub_domain

    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search", "arguments": args}
    }).encode()

    req = urllib.request.Request(URL, data=body, headers={
        "Content-Type": "application/json", "User-Agent": "argo-anysearch/1.0"
    })

    api_key = os.environ.get("ANYSEARCH_API_KEY", "")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e), "results": []}

    content = data.get("result", {}).get("content", [])
    results = []
    for item in content:
        text = item.get("text", "") if isinstance(item, dict) else str(item)
        # anysearch 返回 Markdown: "## Search Results (N results, Xms)\n\n### 1. Title\n- **URL**: ..."
        # 按 "### N. " 分割多个结果
        import re
        blocks = re.split(r'\n### \d+\.\s', text)
        for block in blocks[1:]:  # 跳过开头 "## Search Results..."
            lines = block.strip().split("\n")
            title = lines[0].strip() if lines else ""
            url = ""
            snippet_lines = []
            for line in lines[1:]:
                if line.strip().startswith("- **URL**: "):
                    url = line.strip().replace("- **URL**: ", "")
                elif line.strip().startswith("**URL**: "):
                    url = line.strip().replace("**URL**: ", "")
                else:
                    snippet_lines.append(line)
            snippet = "\n".join(snippet_lines).strip()[:500]
            if title:
                results.append({
                    "title": title[:200],
                    "url": url,
                    "snippet": snippet,
                    "source": "anysearch",
                    "score": 0.7
                })

    return {"results": results, "count": len(results)}

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("subcommand", choices=["search"])
    p.add_argument("query")
    p.add_argument("--max_results", "-n", type=int, default=5)
    p.add_argument("--domain", default="")
    p.add_argument("--sub_domain", default="")
    p.add_argument("--json", action="store_true", default=True)
    args = p.parse_args()

    result = search(args.query, args.max_results, args.domain, args.sub_domain)
    print(json.dumps(result, ensure_ascii=False, indent=2))
