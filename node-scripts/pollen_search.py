"""DDG web search runner for Pollen, executed on a residential GPU node.

Lives at ~/pollen_search.py on the 3090 (search engines block the EC2 hub's
datacenter IP, so the hub calls this over the reverse SSH tunnel; see
rag_fetchers.node_search). This repo copy is the canonical version: after
editing, deploy with

    ssh ubuntu@HUB "ssh -p 31333 irfan@localhost 'cat > ~/pollen_search.py'" \
        < node-scripts/pollen_search.py

Usage: pollen_search.py QUERY [MAX_RESULTS] [TIMELIMIT]
  TIMELIMIT is ddgs's recency window: d (day), w (week), m (month), y (year).
  Omitted or anything else means no time filter. Output: JSON list of
  {title, url, snippet} on stdout; errors go to stderr and stdout stays [].
"""
import sys, json
from ddgs import DDGS


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else ''
    n = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 8
    timelimit = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] in ('d', 'w', 'm', 'y') else None
    out = []
    try:
        with DDGS() as d:
            raw = list(d.text(query, max_results=n, timelimit=timelimit))
        for r in raw:
            out.append({
                'title': r.get('title', ''),
                'url': r.get('href', ''),
                'snippet': r.get('body', ''),
            })
    except Exception as e:
        print(json.dumps({'error': '%s: %s' % (type(e).__name__, e)}), file=sys.stderr)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == '__main__':
    main()
