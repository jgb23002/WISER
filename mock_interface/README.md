# WISER Mock Interface

Open `index.html` directly in a browser to present the WISER workflow without
installing any packages.

Optional local server from the repository root:

```bash
python3 -m http.server 8092
```

Then open:

```text
http://localhost:8092/mock_interface/index.html
```

The interface is a static presentation mock for:

- TaskRoutingAgent route selection
- SQLite outage analytics
- Local RAG knowledge retrieval
- External NOAA/NHC/NCEI storm-impact context
- Optional OpenAI summaries with local fallback
- Multi-dimensional storm similarity search
