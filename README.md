# BacklogSweeper

A local web tool that accelerates Jira backlog cleanup. Displays all backlog issues from selected sprints in a single view with staleness indicators, bulk actions, and one-click close — then syncs changes to Jira in batch.

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![Vanilla JS](https://img.shields.io/badge/JS-Vanilla-yellow) ![Jira](https://img.shields.io/badge/Jira-Server-blue)

## Quick Start

```bash
# 1. Add your Jira credentials to .mcp.json (see below)
# 2. Start the server
python backlog-server.py

# 3. Open in Chrome
# Navigate to backlog-sweep.html (file:// or served)
```

The startup overlay checks connectivity to the server, Jira, and Docker. Once all green, configure your board in Settings (gear icon).

## Prerequisites

- **Python 3.10+**
- **Docker Desktop** running (for MCP Jira container)
- **Jira Server** with a personal access token
- `.mcp.json` in the project root:

```json
{
  "mcpServers": {
    "mcp-jira": {
      "env": {
        "JIRA_URL": "https://your-jira-instance.com",
        "JIRA_PERSONAL_TOKEN": "your-token-here"
      }
    }
  }
}
```

## Features

### Triage at a Glance
- **Staleness indicators** — Created and Updated columns with color-coded age dots: green (<30d), amber (30-90d), red (>90d)
- **Summary stats** — Total issues, bugs, stories, stale count, story points
- **Team filtering** — Only shows issues matching your configured team

### Fast Cleanup Actions
- **Quick Close** — One-click x button per row shows resolution picker directly (Won't Do, Duplicate, Done, Cannot Reproduce, Retired). No transition selection needed. Issues in any workflow state can be closed — the server auto-chains through intermediate transitions when needed
- **Bulk Close** — Select multiple issues, click Close, pick a resolution (defaults to Retired), and queue them all at once
- **Drag-and-drop** — Move issues between sprints or to/from the board backlog

### Issue Detail Panel
- Click any row to open a Jira-like detail view (description, comments, links, attachments, dates, people, agile)
- Development section with linked branches, commits, and pull requests
- Resizable split layout with draggable divider
- Loaded on demand, cached locally

### Pending Changes
- All changes (moves, edits, transitions) are queued locally
- Fixed footer panel — always visible at the bottom, never hides content
- **Apply to Jira** syncs everything with a progress bar
- Closed issues disappear from the board in real time as each transition succeeds
- Persisted in localStorage — survives page reloads

### Organization
- **Sticky toolbar** — Stats, search, sort, and bulk actions stay visible while scrolling
- **Collapsible sections** — Click chevron to collapse backlogs to just their header
- **Per-section type filters** — Show/hide issue types per backlog; toggle closed items
- **Sortable columns** — Key, Epic, Type, Reporter, SP, Priority, Status, Created, Updated
- **Global sort** — By last updated, created date, or story points
- **Search** — Live text filter across all backlogs
- **Draggable section order** — Reorder backlog cards by dragging headers

## Configuration

After first launch, click the gear icon to configure:

1. **Jira Project** — e.g. `FDATA`
2. **Team** — Dropdown populated from Jira's Team field
3. **Board** — Auto-searched by team name
4. **Backlog Sprints** — Check which sprints (+ board backlog) to display

Settings are saved to `team-config.json` (gitignored).

## Architecture

```
Browser (file://)          backlog-server.py (:5001)          Jira Server
  backlog-sweep.html  ──────►  Python HTTP server  ──────────►  REST API
  (vanilla JS/CSS)     fetch    (http.server)        urllib      (Bearer token)
```

- **No npm, no frameworks** — corporate network blocks Node.js
- **Single HTML file** — all CSS and JS embedded
- **JSON file storage** — config and preferences
- **localStorage** — pending changes survive reloads

## File Structure

```
BacklogSweeper/
  backlog-sweep.html    # Single-page frontend
  backlog-server.py     # Python HTTP server (port 5001)
  issue-types.json      # Known issue types for filters
  icons/                # Cached Jira priority icons
  .mcp.json             # Jira credentials (gitignored)
  team-config.json      # Board/team settings (gitignored)
  backlog-prefs.json    # Selections, order, filters (gitignored)
  CLAUDE.md             # Detailed project documentation
```

## Related

Built alongside [SprintPlanner](../SprintPlanner) — shares Jira integration patterns but focuses exclusively on backlog triage rather than capacity planning.
