# BacklogSweeper

Accelerates Jira backlog cleanup with a drag-and-drop web UI. Shows staleness indicators, supports bulk actions, and syncs changes to Jira in real time.

## What It Does

Presents all backlog issues from selected Jira sprints in a single view with age/staleness indicators, bulk close/move/priority actions, and one-click issue closure. Changes are queued locally and applied to Jira in batch.

## Tech Stack

- **Backend**: Python 3.10+ HTTP server (`backlog-server.py`) on port 5001
- **Frontend**: Single HTML file (`backlog-sweep.html`) with embedded CSS/JS — vanilla JavaScript, no frameworks, no npm
- **Jira Integration**: Direct REST API via Bearer token from `.mcp.json`
- **Data Storage**: JSON files for config and preferences

## How to Run

1. `python backlog-server.py` — starts on http://localhost:5001
2. Open `backlog-sweep.html` in Chrome
3. A startup overlay checks server, Jira, and Docker health — tasks load automatically once all green
4. If no board is configured, Settings opens automatically

## Configuration

Settings stored in `team-config.json` (gitignored):

```json
{
  "project_key": "FDATA",
  "board_id": 17259,
  "board_url": "https://jira.autodesk.com/secure/RapidBoard.jspa?rapidView=17259",
  "board_name": "Gemini",
  "team_name": "Gemini",
  "team": ["Person A", "Person B"]
}
```

Settings are managed via the gear icon in the UI header.

## Jira Integration

- Jira instance URL and token are read from `.mcp.json` (same format as SprintPlanner)
- **Jira fields**: SP = `customfield_10130`, Epic Link = `customfield_12780`, Team = `customfield_19700`
- Board/team selection works by: (1) selecting a Team from a dropdown populated from Jira's Team field, (2) searching Jira Scrum boards matching that team name
- Backlog sprints (active + future) and the board backlog are selected in Settings and appear as card sections
- Issues are filtered by the configured team name — only issues whose Team field matches appear

## Key Features

### Staleness Indicators
- **Created** and **Updated** columns show relative time with color-coded age dots (e.g. "45d")
- Color-coded: green (<30d), amber (30-90d), red (>90d)
- Rows not updated in >90 days get a red left-border highlight

### Bulk Actions
- Checkbox per issue row
- Toolbar: Move to sprint, Close — all queue as pending changes
- Bulk close dialog shows resolution picker only (default: Retired); transition is always Close Issue

### Quick Close
- Close button (x) per row shows resolution picker directly (Won't Do, Duplicate, Done, Cannot Reproduce, Retired)
- No transition selection needed — always Close Issue
- Queued as pending transitions alongside moves and edits
- Successfully closed issues are removed from the board in real time as each transition completes
- **Auto-chaining**: Issues in any state can be closed — the server automatically chains through intermediate states when no direct Close transition exists. Resolve/Done are never used as intermediate hops (reserved for normal completion)
- If a transition screen rejects the resolution field, the server retries without it

### Workflow Transition Chains
The server resolves transitions by name (not just ID) since IDs vary by state:
| Current State | Path to Closed |
|---|---|
| Open | Close Issue — direct |
| In Progress | Close Issue — direct |
| Resolved | Close Issue — direct |
| Reopened | Close Issue — direct |
| In Review | Start Progress → Close Issue |
| Blocked | Start Progress → Close Issue |
| To Do | Open → Close Issue |

### Pending Jira Changes
- Three types: moves, edits (SP/priority), transitions (close/resolve)
- All persisted in localStorage (`backlogsweeper-pending`)
- Fixed footer panel at the bottom of the left pane — always visible, never overlaps content
- Apply to Jira syncs all with progress bar and per-item status
- Transitioned issues disappear immediately on success; counters update in real time
- Discard All reverts all pending changes

### Issue Detail Panel
- Click any issue row to open a Jira-like detail view on the right
- Two-column layout: Details/Description/Comments/Links/Dev/Attachments on the left; People/Dates/Agile on the right
- Development section shows linked branches, commits, and pull requests with clickable URLs
- Resizable split between backlog list and detail panel (drag the divider)
- Loaded on demand, cached client-side
- Close with X button or Escape key

### Board Backlog Support
- The Jira board backlog (issues not in any sprint) appears as a selectable "Backlog" section
- Issues can be dragged to/from the board backlog like any sprint
- Moving to backlog uses `POST /rest/agile/1.0/backlog/issue`

### UI Layout
- **Compact header**: Title and board name on a single line
- **Sticky toolbar**: Stats bar and search/sort/bulk actions stick to the top while scrolling backlogs
- **Three-zone left pane**: Fixed toolbar (top) → scrollable backlogs (middle) → fixed pending panel (bottom)

### Other Features
- **Drag-and-drop**: Move issues between backlog sections (empty sections show drop placeholder)
- **Right-click context menu**: Move without dragging
- **Editable fields**: SP (inline), Priority (icon dropdown)
- **Per-section type filter**: Funnel icon with checkbox popover (types from `issue-types.json`); includes toggle to show/hide closed items
- **Collapsible sections**: Click chevron to collapse a backlog to just its header; state persisted across reloads
- **Epic expansion**: Click epic to see children inline
- **Sortable columns**: Key, Epic, Type, Reporter, SP, Priority, Status, Created, Updated
- **Global sort**: By last updated, created date, SP
- **Search**: Live text filter across all backlogs
- **Summary stats**: Total items, bugs, stories, stale count, total SP
- **Team filtering**: Issues are filtered by the configured team name from Jira's Team field

## API Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/health` | GET | Server + Jira + Docker health check; returns `jira_url` |
| `/api/config` | GET/POST | Read/save team config |
| `/api/boards` | GET | Search Jira scrum boards |
| `/api/project-teams` | GET | List team names from Jira Team field |
| `/api/board-sprints` | GET | All sprints (active + future + board backlog) for a board |
| `/api/sprint-info` | GET | Board config + all sprint names |
| `/api/issue-detail` | GET | Full issue detail with rendered fields, comments, dev status |
| `/api/sprint-issues` | GET | Issues with updated/created timestamps; supports `backlog` token |
| `/api/epic-children` | GET | Epic children with updated/created |
| `/api/backlog-prefs` | GET/POST | Backlog selections, order, filters, collapse state |
| `/api/issue-types` | GET | Static issue type list |
| `/api/transitions` | GET | Available workflow transitions for an issue |
| `/api/move` | POST | Move issue to sprint or board backlog |
| `/api/edit` | POST | Edit SP/assignee/priority |
| `/api/transition` | POST | Execute workflow transition |
| `/api/bulk-move` | POST | Batch move issues |
| `/api/bulk-transition` | POST | Batch transition issues |
| `/icons/*` | GET | Priority icon files |

## Jira Fields Written by Apply to Jira

- Sprint move → `POST /rest/agile/1.0/sprint/{id}/issue`
- Backlog move → `POST /rest/agile/1.0/backlog/issue`
- SP → `customfield_10130` + `timetracking.originalEstimate` (1 SP = 1 day); SP=0 → `4h`
- Assignee → resolved via user search
- Priority → `priority.id` or `priority.name`
- Transition → `POST /rest/api/2/issue/{key}/transitions` with optional resolution

## Constraints

- Never commit `.mcp.json` — contains the Jira personal token
- Never commit `team-config.json` — contains team-specific settings
- This project uses Python + vanilla JS by design (single-file frontend, no build step)

## Relationship to SprintPlanner

BacklogSweeper reuses Jira integration patterns from SprintPlanner (`C:\dev\SprintPlanner`) but excludes all capacity-related features: team capacity table, efficiency overrides, deductions, spillover, PA/PR schedules, Workday integration, holiday calendar, and Sprint Commitment.

## File Reference

| File | Purpose |
|---|---|
| `backlog-sweep.html` | Interactive backlog cleanup UI (single-page app) |
| `backlog-server.py` | Local HTTP server bridging UI to Jira |
| `team-config.json` | Board and team settings (gitignored) |
| `backlog-prefs.json` | Saved backlog selections, order, filters, and collapse state |
| `issue-types.json` | Static list of known issue types |
| `.mcp.json` | Jira credentials (gitignored, shared format with SprintPlanner) |
| `icons/` | Cached Jira priority icon files |
