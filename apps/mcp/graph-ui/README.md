# Graph Studio — `apps/mcp/graph-ui/`

Graph Studio is the browser-based visual graph editor bundled with Waggle. It is built with **Vite + React + Tailwind CSS** and served locally by `waggle-mcp edit-graph`.

---

## Features

- **Dual-layer view** — switch between graph topology and conversation transcript views
- **Interactive editing** — drag nodes, shift-drag to create edges, click to edit content
- **Retrieval debug** — inspect hybrid retrieval scores, transcript provenance, and node ranking
- **Live stats** — connected nodes, isolate count, cluster summary
- **Export/import** — export the current graph view, preview `.abhi` diffs, and trigger sharing workflows
- **Collapsible panels** — focus mode for dense graphs; label toggle for readability

---

## Quick Start (Development)

```bash
# From the repo root
cd apps/mcp/graph-ui
npm install
npm run dev
```

The dev server starts at `http://localhost:5173`. It expects the Waggle MCP server to be running separately:

```bash
# In another terminal
WAGGLE_MODEL=deterministic waggle-mcp serve --transport http
```

---

## Build for Production

The built assets are bundled into the Python package under `src/waggle/static/graph/`:

```bash
cd apps/mcp/graph-ui
npm run build
# Output goes to: ../../../src/waggle/static/graph/
```

After building, reinstall the package to pick up the new assets:

```bash
pip install -e .
```

---

## Tech Stack

| Tool | Version | Purpose |
|---|---|---|
| [Vite](https://vitejs.dev/) | ^5 | Build tool + dev server |
| [React](https://react.dev/) | ^18 | UI framework |
| [Tailwind CSS](https://tailwindcss.com/) | ^3 | Utility-first styling |
| [PostCSS](https://postcss.org/) | ^8 | CSS transformation pipeline |

---

## Configuration

| File | Purpose |
|---|---|
| `vite.config.js` | Build output path, dev proxy config |
| `tailwind.config.js` | Tailwind content paths and theme extensions |
| `postcss.config.js` | PostCSS plugins (autoprefixer) |

---

## Environment Variables (Dev)

The dev server reads these from the shell or a `.env` file in `apps/mcp/graph-ui/`:

| Variable | Default | Description |
|---|---|---|
| `VITE_WAGGLE_API_URL` | `http://localhost:8080` | Base URL for the Waggle HTTP API |

---

## URL Parameters

Graph Studio supports the following URL query parameters:

| Parameter | Values | Default | Description |
|---|---|---|---|
| `mode` | `edit`, `view` | `edit` | View-only mode hides editing and deletion functions. |
| `tab` | `graph`, `transcripts`, `retrieval` | `graph` | Selects which tab to activate first on load. |

---

## Screenshots

> Run `waggle-mcp edit-graph` to launch Graph Studio in your browser.
