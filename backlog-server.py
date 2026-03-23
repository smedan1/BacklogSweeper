"""
Backlog Sweeper server — local HTTP proxy for triaging and cleaning up Jira backlogs.

Usage:
    python backlog-server.py

Runs on http://localhost:5001
Reads config from .mcp.json in the same directory.

API:
    GET  /api/health                     — liveness + service connectivity check
    GET  /api/config                     — return team config
    GET  /api/boards?project=X&name=Y   — search scrum boards
    GET  /api/project-teams?project=X   — list team names
    GET  /api/board-sprints?board_id=N  — return all sprints (active + future)
    GET  /api/sprint-issues?sprints=X,Y — fetch issues with updated/created fields
    GET  /api/epic-children?key=X       — fetch epic children with updated/created
    GET  /api/backlog-prefs             — read backlog-prefs.json
    GET  /api/issue-types               — read issue-types.json
    GET  /api/transitions?key=X         — get available transitions for an issue
    GET  /icons/*                       — serve priority icon files
    POST /api/config                    — save team config
    POST /api/backlog-prefs             — save backlog prefs
    POST /api/edit                      — edit SP/assignee/priority
    POST /api/move                      — move issue to sprint
    POST /api/transition                — execute a workflow transition
    POST /api/bulk-move                 — batch move issues to a sprint
    POST /api/bulk-transition           — batch transition issues
"""

import json
import os
import re
import subprocess
import time
import urllib.request
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── Config ────────────────────────────────────────────────────────────────────

PORT       = 5001
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MCP_JSON   = os.path.join(SCRIPT_DIR, '.mcp.json')


def load_config():
    with open(MCP_JSON, 'r') as f:
        cfg = json.load(f)
    env = cfg['mcpServers']['mcp-jira']['env']
    return env['JIRA_URL'], env['JIRA_PERSONAL_TOKEN']


JIRA_URL, JIRA_TOKEN = load_config()


# ── Jira priorities cache ─────────────────────────────────────────────────────

_PRIORITIES_CACHE: list[dict] | None = None


_PRI_ALIASES = {'Standard': 'Minor'}

def _strip_pri_name(name: str) -> str:
    """Strip leading number prefix from Jira priority names and normalize aliases."""
    stripped = re.sub(r'^\d+[\s.\-]+\s*', '', name)
    return _PRI_ALIASES.get(stripped, stripped)


def _download_priority_icon(icon_url: str, pri_name: str) -> str:
    """Download a Jira priority icon and save to icons/ dir. Returns local relative path."""
    icons_dir = os.path.join(SCRIPT_DIR, 'icons')
    os.makedirs(icons_dir, exist_ok=True)
    safe_name = re.sub(r'[^a-z0-9]', '-', pri_name.lower()).strip('-')
    # Detect extension from URL
    ext = '.png'
    if '.svg' in icon_url:
        ext = '.svg'
    elif '.gif' in icon_url:
        ext = '.gif'
    local_name = f'priority-{safe_name}{ext}'
    local_path = os.path.join(icons_dir, local_name)
    if os.path.exists(local_path):
        return f'icons/{local_name}'
    try:
        req = urllib.request.Request(icon_url, headers={'Authorization': f'Bearer {JIRA_TOKEN}'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        with open(local_path, 'wb') as f:
            f.write(data)
        return f'icons/{local_name}'
    except Exception as e:
        print(f'  x Failed to download priority icon {icon_url}: {e}')
        return ''


def fetch_jira_priorities() -> list[dict]:
    """Fetch all priority levels from Jira. Cached after first call."""
    global _PRIORITIES_CACHE
    if _PRIORITIES_CACHE is not None:
        return _PRIORITIES_CACHE
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/priority',
        method='GET',
        headers={'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode('utf-8'))
        result = []
        seen_names = set()
        for p in raw:
            name = _strip_pri_name(p.get('name', ''))
            if name in seen_names:
                continue
            seen_names.add(name)
            icon_url = p.get('iconUrl', '')
            local_icon = _download_priority_icon(icon_url, name) if icon_url else ''
            result.append({
                'id': p.get('id', ''),
                'name': name,
                'iconUrl': icon_url,
                'localIcon': local_icon,
            })
        _PRIORITIES_CACHE = result
        print(f'  > Fetched {len(result)} Jira priorities')
        return result
    except Exception as e:
        print(f'  x Failed to fetch priorities: {e}')
        return []


# ── Jira API calls ────────────────────────────────────────────────────────────

def check_jira_health() -> tuple[bool, str]:
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/serverInfo',
        method='GET',
        headers={'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status < 300, ''
    except urllib.error.HTTPError as e:
        return False, f'HTTP {e.code}'
    except Exception as e:
        return False, str(e)


def check_docker() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ['docker', 'info', '--format', '{{.ServerVersion}}'],
            capture_output=True, timeout=5
        )
        if result.returncode == 0:
            return True, ''
        return False, result.stderr.decode('utf-8', errors='replace').strip().splitlines()[0]
    except FileNotFoundError:
        return False, 'docker not found in PATH'
    except subprocess.TimeoutExpired:
        return False, 'timed out'
    except Exception as e:
        return False, str(e)


TEAM_FIELD_ID = 'customfield_19700'  # "Team" multiselect on FDATA issues


def _resolve_epic_names(epic_keys: list[str]) -> dict[str, str]:
    """Batch-fetch epic summaries by key. Returns {key: summary}."""
    if not epic_keys:
        return {}
    jql = 'key in (' + ','.join(epic_keys) + ')'
    params = urllib.parse.urlencode({
        'jql': jql,
        'fields': 'summary',
        'maxResults': len(epic_keys),
    })
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/search?{params}',
        method='GET',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        return {i['key']: i['fields'].get('summary', '') for i in body.get('issues', [])}
    except Exception as e:
        print(f'  x Failed to resolve epic names: {e}')
        return {}


def get_issues_for_sprint(sprint_id: int) -> list[dict]:
    """Fetch all issues in a sprint with SP, assignee, metadata, updated, and created.
    Does NOT filter out closed/resolved/done issues (BacklogSweeper needs to see all)."""
    params = urllib.parse.urlencode({
        'jql': f'sprint = {sprint_id} ORDER BY created ASC',
        'fields': 'customfield_10130,customfield_12780,customfield_19700,assignee,reporter,summary,issuetype,status,priority,updated,created',
        'maxResults': 500,
    })
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/search?{params}',
        method='GET',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        result = []
        for i in body.get('issues', []):
            fields   = i.get('fields', {})
            assignee = fields.get('assignee') or {}
            reporter = fields.get('reporter') or {}
            pri      = fields.get('priority') or {}
            pri_name = _strip_pri_name(pri.get('name', ''))
            epic_raw = fields.get('customfield_12780')
            epic_key = ''
            if isinstance(epic_raw, str):
                epic_key = epic_raw
            elif isinstance(epic_raw, dict):
                epic_key = epic_raw.get('value', '') or epic_raw.get('key', '')
            # Extract team values from multiselect field
            team_raw = fields.get('customfield_19700') or []
            team_values = []
            for opt in (team_raw if isinstance(team_raw, list) else []):
                v = opt.get('value', '') if isinstance(opt, dict) else str(opt)
                if v:
                    team_values.append(v)
            result.append({
                'key':              i['key'],
                'summary':          fields.get('summary', ''),
                'sp':               fields.get('customfield_10130'),
                'assignee_display': assignee.get('displayName') or '',
                'assignee_name':    assignee.get('name') or '',
                'reporter_display': reporter.get('displayName') or '',
                'type':             (fields.get('issuetype') or {}).get('name', 'Story'),
                'status':           (fields.get('status')    or {}).get('name', ''),
                'priority':         pri_name,
                'priority_id':      pri.get('id', ''),
                'priority_icon':    pri.get('iconUrl', ''),
                'sprint_id':        sprint_id,
                'epic_key':         epic_key,
                'epic_name':        '',
                'updated':          fields.get('updated', ''),
                'created':          fields.get('created', ''),
                'team':             team_values,
            })
        # Batch-resolve epic names
        epic_keys = list(set(r['epic_key'] for r in result if r['epic_key']))
        if epic_keys:
            epic_names = _resolve_epic_names(epic_keys)
            for r in result:
                if r['epic_key']:
                    r['epic_name'] = epic_names.get(r['epic_key'], '')
        return result
    except Exception:
        return []


def get_epic_children(epic_key: str) -> list[dict]:
    """Fetch child issues of an epic from Jira.
    Does NOT filter out closed/resolved/done issues (BacklogSweeper needs to see all)."""
    jql = f'"Epic Link" = {epic_key} OR parent = {epic_key} ORDER BY priority ASC, created ASC'
    params = urllib.parse.urlencode({
        'jql': jql,
        'fields': 'customfield_10130,assignee,reporter,summary,issuetype,status,priority,sprint,updated,created',
        'maxResults': 200,
    })
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/search?{params}',
        method='GET',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        result = []
        for i in body.get('issues', []):
            fields = i.get('fields', {})
            assignee = fields.get('assignee') or {}
            reporter = fields.get('reporter') or {}
            pri = fields.get('priority') or {}
            pri_name = _strip_pri_name(pri.get('name', ''))
            # Extract sprint info
            sprints = []
            sprint_field = fields.get('sprint')
            if sprint_field and isinstance(sprint_field, dict):
                sprints.append({
                    'id': sprint_field.get('id'),
                    'name': sprint_field.get('name', ''),
                })
            result.append({
                'key':              i['key'],
                'summary':          fields.get('summary', ''),
                'sp':               fields.get('customfield_10130'),
                'assignee_display': assignee.get('displayName') or '',
                'assignee_name':    assignee.get('name') or '',
                'reporter_display': reporter.get('displayName') or '',
                'type':             (fields.get('issuetype') or {}).get('name', 'Story'),
                'status':           (fields.get('status')    or {}).get('name', ''),
                'priority':         pri_name,
                'priority_id':      pri.get('id', ''),
                'priority_icon':    pri.get('iconUrl', ''),
                'sprints':          sprints,
                'updated':          fields.get('updated', ''),
                'created':          fields.get('created', ''),
            })
        return result
    except Exception as e:
        print(f'  x Failed to fetch epic children for {epic_key}: {e}')
        return []


def _sp_to_estimate(sp: float | None) -> str:
    """Convert story points (1 SP = 1 day) to a Jira time string, e.g. 3->'3d', 0.5->'4h'."""
    if not sp:
        return ''
    days  = int(sp)
    hours = round((sp - days) * 8)
    if days and hours:
        return f'{days}d {hours}h'
    if days:
        return f'{days}d'
    return f'{hours}h'


def _parse_estimate_to_sp(estimate_str: str, fallback_secs: int) -> float:
    """Parse Jira estimate string (e.g. '3d', '3d 4h', '4h') to SP (1 SP = 1 day).
    Uses the string to avoid dependency on Jira's hours-per-day config.
    Falls back to seconds / 21600 (6h workday) if parsing fails."""
    if estimate_str:
        days = 0.0
        m = re.search(r'(\d+(?:\.\d+)?)\s*[wW]', estimate_str)
        if m:
            days += float(m.group(1)) * 5
        m = re.search(r'(\d+(?:\.\d+)?)\s*[dD]', estimate_str)
        if m:
            days += float(m.group(1))
        m = re.search(r'(\d+(?:\.\d+)?)\s*[hH]', estimate_str)
        if m:
            days += float(m.group(1)) / 8  # 8h per SP regardless of Jira config
        m = re.search(r'(\d+(?:\.\d+)?)\s*[mM]', estimate_str)
        if m:
            days += float(m.group(1)) / 480  # 480 min = 8h
        if days > 0:
            return round(days, 1)
    # Fallback: Jira instance uses 6h workdays (21600s/day)
    return round(fallback_secs / 21600, 1) if fallback_secs > 0 else 0


def get_time_spent(issue_key: str) -> int:
    """Returns timeSpentSeconds for the issue. Returns 0 on error or no logged work."""
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/issue/{issue_key}?fields=timetracking',
        method='GET',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        tt = (body.get('fields') or {}).get('timetracking') or {}
        return tt.get('timeSpentSeconds') or 0
    except Exception:
        return 0


def _secs_to_estimate(secs: int) -> str:
    """Convert seconds to a Jira time string (1 day = 8 h). Returns '0d' for zero."""
    if secs <= 0:
        return '0d'
    days  = secs // 28800
    hours = (secs % 28800) // 3600
    if days and hours:
        return f'{days}d {hours}h'
    if days:
        return f'{days}d'
    return f'{hours}h'


def update_issue_fields(issue_key: str, fields: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/issue/{issue_key}',
        data=json.dumps({'fields': fields}).encode('utf-8'),
        method='PUT',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, 'ok'
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='replace')
    except Exception as e:
        return 0, str(e)


def find_user_name(display_name: str) -> tuple[str, str]:
    """Search Jira Server for a user by display name.
    Returns (username, error_message). On success error_message is empty."""
    # Jira Server uses 'username' param; Jira Cloud uses 'query'
    params = urllib.parse.urlencode({'username': display_name, 'maxResults': 5})
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/user/search?{params}',
        method='GET',
        headers={'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            users = json.loads(resp.read().decode('utf-8'))
        if not users:
            return '', f"No Jira user found matching '{display_name}'"
        # Prefer exact display name match, else take first result
        for u in users:
            if u.get('displayName', '').lower() == display_name.lower():
                return u['name'], ''
        return users[0]['name'], ''
    except urllib.error.HTTPError as e:
        return '', f'User search HTTP {e.code}'
    except Exception as e:
        return '', str(e)


def move_issue_to_backlog(issue_key: str) -> tuple[int, str]:
    """Move an issue to the board backlog (remove from all sprints)."""
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/agile/1.0/backlog/issue',
        data=json.dumps({'issues': [issue_key]}).encode('utf-8'),
        method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, 'ok'
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='replace')
    except Exception as e:
        return 0, str(e)


def move_issue_to_sprint(issue_key: str, sprint_id: int) -> tuple[int, str]:
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/agile/1.0/sprint/{sprint_id}/issue',
        data=json.dumps({'issues': [issue_key]}).encode('utf-8'),
        method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, 'ok'
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='replace')
    except Exception as e:
        return 0, str(e)


# ── Transitions ───────────────────────────────────────────────────────────────

def get_transitions(issue_key: str) -> tuple[int, list[dict]]:
    """Get available workflow transitions for an issue.
    Returns (status_code, list of {id, name, to: {name}})."""
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/issue/{issue_key}/transitions',
        method='GET',
        headers={'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        transitions = []
        for t in body.get('transitions', []):
            transitions.append({
                'id': t.get('id', ''),
                'name': t.get('name', ''),
                'to': {'name': (t.get('to') or {}).get('name', '')},
            })
        return 200, transitions
    except urllib.error.HTTPError as e:
        e.read()
        return e.code, []
    except Exception:
        return 0, []


def _do_transition(issue_key: str, transition_id: str, resolution: str | None = None) -> tuple[int, str]:
    """Execute a single workflow transition. Low-level — use transition_issue() instead.
    If resolution field is rejected by the transition screen, retries without it."""
    payload: dict = {'transition': {'id': transition_id}}
    if resolution:
        payload['fields'] = {'resolution': {'name': resolution}}
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/issue/{issue_key}/transitions',
        data=json.dumps(payload).encode('utf-8'),
        method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, 'ok'
    except urllib.error.HTTPError as e:
        err_body = e.read().decode('utf-8', errors='replace')
        # If resolution field was rejected, retry without it
        if resolution and 'resolution' in err_body.lower() and 'cannot be set' in err_body.lower():
            print(f'  > {issue_key}: resolution field rejected, retrying without it')
            payload2: dict = {'transition': {'id': transition_id}}
            req2 = urllib.request.Request(
                f'{JIRA_URL}/rest/api/2/issue/{issue_key}/transitions',
                data=json.dumps(payload2).encode('utf-8'),
                method='POST',
                headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {JIRA_TOKEN}'}
            )
            try:
                with urllib.request.urlopen(req2, timeout=10) as resp2:
                    return resp2.status, 'ok'
            except urllib.error.HTTPError as e2:
                return e2.code, e2.read().decode('utf-8', errors='replace')
            except Exception as e2:
                return 0, str(e2)
        return e.code, err_body
    except Exception as e:
        return 0, str(e)


def _find_transition(available: list[dict], transition_id: str, transition_name: str) -> dict | None:
    """Find a matching transition from the available list, by ID then by name."""
    # Exact ID match
    for t in available:
        if t['id'] == transition_id:
            return t
    # Exact name match
    if transition_name:
        target = transition_name.lower().strip()
        for t in available:
            if t['name'].lower().strip() == target:
                return t
        # Fuzzy substring match (e.g. "Close" matches "Close Issue")
        for t in available:
            if target in t['name'].lower() or t['name'].lower() in target:
                return t
    return None


def _find_close_transition(available: list[dict]) -> dict | None:
    """Find any close/resolve transition from the available list.
    Priority: Close Issue > Resolve Issue > anything leading to a done state."""
    for keyword in ('close', 'resolve', 'done'):
        for t in available:
            if keyword in t['name'].lower():
                return t
    return None


def transition_issue(issue_key: str, transition_id: str, resolution: str | None = None,
                     transition_name: str = '') -> tuple[int, str]:
    """Execute a workflow transition on an issue.

    Handles two problems:
    1. Transition IDs vary by current state — resolves by name when ID doesn't match.
    2. Some states (To Do, In Review, Blocked) have no direct Close transition —
       automatically chains through intermediate states (e.g. Open → Close, or
       Resolve → Close) with up to 3 hops.

    Transition map for FDATA project:
        Open:        Close(2), Resolve(5), Start Progress(4), To Do(741)
        In Progress: Close(2), Resolve(5), Blocked(711), In Review(721), To Do(741)
        To Do:       Open(341), Blocked(431), In Design(401), Ready for Dev(421), Back to Progress(391) — NO CLOSE
        In Review:   Resolve(5), Start Progress(4) — NO CLOSE
        Blocked:     Start Progress(4) — NO CLOSE
        Resolved:    Close(701), Reopen(3), In Review(731)
        Reopened:    Close(2), Resolve(5), Start Progress(4)
    """

    # Fetch what's actually available for THIS issue in its current state
    status, available = get_transitions(issue_key)
    if status != 200:
        return _do_transition(issue_key, transition_id, resolution)

    # Try to find the requested transition (by ID or name)
    match = _find_transition(available, transition_id, transition_name)
    if match:
        if match['id'] != transition_id:
            print(f'  > {issue_key}: resolved "{transition_name}" to id {match["id"]} (was {transition_id})')
        return _do_transition(issue_key, match['id'], resolution)

    # Not available — need to chain through intermediate states.
    # Strategy: find the closest path to a state that has Close/Resolve.
    print(f'  > {issue_key}: "{transition_name}" not available ({[t["name"] for t in available]}), chaining...')

    for hop in range(3):
        # Re-fetch available transitions (state changed from previous hop)
        if hop > 0:
            status, available = get_transitions(issue_key)
            if status != 200:
                return status, 'Failed to fetch transitions during chain'

        # Check if our target is now available
        match = _find_transition(available, transition_id, transition_name)
        if match:
            print(f'  > {issue_key}: target available after {hop} hop(s)')
            return _do_transition(issue_key, match['id'], resolution)

        # Find the best available transition to get closer to Closed.
        # Priority: "close" (terminal) > "resolve" (one hop from close) > "open"/"start progress" (two hops)
        # Priority: close (terminal) > start/back to progress (In Progress has close)
        #           > open (Open has close) > reopen
        # Never use resolve/done as intermediate — those are for normal completion only.
        best = None
        best_score = 99
        for t in available:
            tn = t['name'].lower()
            score = 99
            if 'close' in tn:
                score = 0  # direct close — terminal
            elif 'start progress' in tn or 'back to progress' in tn:
                score = 1  # In Progress has Close available
            elif 'open' in tn and 'reopen' not in tn:
                score = 2  # Open has Close available
            elif 'reopen' in tn:
                score = 3
            # Skip resolve/done — reserved for normal completion
            if score < best_score:
                best_score = score
                best = t

        if not best:
            avail_names = [t['name'] for t in available]
            return 400, f'No path to close for {issue_key} (available: {avail_names})'

        # If this is a close transition (terminal), pass the resolution
        is_terminal = best_score == 0
        hop_resolution = resolution if is_terminal else None
        print(f'  > {issue_key}: hop {hop+1} — "{best["name"]}" (id {best["id"]}){" [terminal]" if is_terminal else ""}')
        s, msg = _do_transition(issue_key, best['id'], hop_resolution)
        if s not in (200, 201, 204):
            return s, f'Chain failed at "{best["name"]}": {msg}'

        # If this was a terminal close, we're done
        if is_terminal:
            return s, 'ok'

        # Otherwise continue the loop — next iteration re-fetches transitions from the new state

    return 400, f'Could not close {issue_key} after 3 hops'


# ── Issue detail ─────────────────────────────────────────────────────────────

def get_issue_detail(issue_key: str) -> tuple[int, dict]:
    """Fetch full issue detail with rendered fields, comments, dev status.
    Returns (status_code, detail_dict)."""
    # 1. Main issue fetch with renderedFields
    params = urllib.parse.urlencode({'expand': 'renderedFields,names'})
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/api/2/issue/{issue_key}?{params}',
        method='GET',
        headers={'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        e.read()
        return e.code, {}
    except Exception:
        return 0, {}

    fields = raw.get('fields', {})
    rendered = raw.get('renderedFields', {})

    # Extract standard fields
    status = fields.get('status') or {}
    issuetype = fields.get('issuetype') or {}
    priority = fields.get('priority') or {}
    assignee = fields.get('assignee')
    reporter = fields.get('reporter')
    resolution = fields.get('resolution')

    # Rendered description (HTML)
    desc_html = rendered.get('description', '') or ''

    # Comments: merge rendered bodies
    raw_comments = (fields.get('comment') or {}).get('comments', [])
    rendered_comments = (rendered.get('comment') or {}).get('comments', [])
    comments = []
    for idx, c in enumerate(raw_comments):
        body_html = ''
        if idx < len(rendered_comments):
            body_html = rendered_comments[idx].get('body', '')
        author = c.get('author') or {}
        comments.append({
            'author': {
                'displayName': author.get('displayName', ''),
                'avatarUrls': author.get('avatarUrls', {}),
            },
            'created': c.get('created', ''),
            'updated': c.get('updated', ''),
            'body_html': body_html,
        })

    # Issue links
    raw_links = fields.get('issuelinks') or []
    issuelinks = []
    for link in raw_links:
        link_type = link.get('type') or {}
        entry = {
            'type': {
                'name': link_type.get('name', ''),
                'inward': link_type.get('inward', ''),
                'outward': link_type.get('outward', ''),
            },
            'inwardIssue': None,
            'outwardIssue': None,
        }
        for direction in ('inwardIssue', 'outwardIssue'):
            linked = link.get(direction)
            if linked:
                ls = (linked.get('fields', {}).get('status') or {}) if 'fields' in linked else (linked.get('status') or {})
                entry[direction] = {
                    'key': linked.get('key', ''),
                    'summary': linked.get('fields', {}).get('summary', '') if 'fields' in linked else linked.get('summary', ''),
                    'status': {'name': ls.get('name', '')},
                }
        issuelinks.append(entry)

    # Attachments
    raw_attachments = fields.get('attachment') or []
    attachments = []
    for att in raw_attachments:
        att_author = att.get('author') or {}
        attachments.append({
            'filename': att.get('filename', ''),
            'size': att.get('size', 0),
            'created': att.get('created', ''),
            'author': {'displayName': att_author.get('displayName', '')},
        })

    # Sprint info — try Agile fields first, fall back to customfield_12380
    sprint_info = None
    closed_sprints = []

    sprint_field = fields.get('sprint')
    if sprint_field and isinstance(sprint_field, dict):
        sprint_info = {'name': sprint_field.get('name', ''), 'state': sprint_field.get('state', '')}
    cs_field = fields.get('closedSprints')
    if cs_field and isinstance(cs_field, list):
        for cs in cs_field:
            if isinstance(cs, dict):
                closed_sprints.append({'name': cs.get('name', ''), 'completeDate': cs.get('completeDate', '')})

    # Fallback: parse customfield_12380 (Jira Server stores sprints as serialized Java strings)
    if not sprint_info and not closed_sprints:
        cf_sprint = fields.get('customfield_12380')
        if cf_sprint:
            sprint_strings = cf_sprint if isinstance(cf_sprint, list) else [cf_sprint]
            for ss in sprint_strings:
                if not isinstance(ss, str):
                    continue
                name_m = re.search(r'name=([^,\]]+)', ss)
                state_m = re.search(r'state=([^,\]]+)', ss)
                if not name_m:
                    continue
                s_name = name_m.group(1).strip()
                s_state = state_m.group(1).strip().lower() if state_m else ''
                entry = {'name': s_name, 'state': s_state}
                if s_state == 'closed':
                    complete_m = re.search(r'completeDate=([^,\]]+)', ss)
                    entry['completeDate'] = complete_m.group(1).strip() if complete_m else ''
                    closed_sprints.append(entry)
                elif not sprint_info:
                    sprint_info = entry

    # Epic link
    epic_raw = fields.get('customfield_12780')
    epic_key = ''
    if isinstance(epic_raw, str):
        epic_key = epic_raw
    elif isinstance(epic_raw, dict):
        epic_key = epic_raw.get('value', '') or epic_raw.get('key', '')

    # Team
    team_raw = fields.get('customfield_19700')
    team = []
    if isinstance(team_raw, list):
        for t in team_raw:
            if isinstance(t, dict):
                team.append({'value': t.get('value', '')})
            else:
                team.append({'value': str(t)})

    # Priority normalization
    pri_name = _strip_pri_name(priority.get('name', ''))

    # 2. Transitions (reuse existing function)
    _, transitions = get_transitions(issue_key)

    # 3. Dev status (best-effort) — fetch detail API for URLs
    dev_info = {'branches': [], 'commits': [], 'pullRequests': []}
    try:
        numeric_id = raw.get('id', '')
        dev_req = urllib.request.Request(
            f'{JIRA_URL}/rest/dev-status/latest/issue/detail?issueId={numeric_id}&applicationType=githube&dataType=repository',
            method='GET',
            headers={'Authorization': f'Bearer {JIRA_TOKEN}'}
        )
        with urllib.request.urlopen(dev_req, timeout=10) as dev_resp:
            dev_data = json.loads(dev_resp.read().decode('utf-8'))
        for detail in dev_data.get('detail', []):
            for repo in detail.get('repositories', []):
                for br in repo.get('branches', []):
                    dev_info['branches'].append({
                        'name': br.get('name', ''),
                        'url': br.get('url', ''),
                    })
                    for commit in br.get('commits', []):
                        dev_info['commits'].append({
                            'id': commit.get('id', '')[:7],
                            'message': commit.get('message', '').split('\n')[0][:80],
                            'url': commit.get('url', ''),
                            'author': commit.get('authorName', ''),
                        })
                for pr in repo.get('pullRequests', []):
                    dev_info['pullRequests'].append({
                        'name': pr.get('name', ''),
                        'url': pr.get('url', ''),
                        'status': pr.get('status', ''),
                    })
    except Exception:
        pass  # dev status is optional

    result = {
        'key': raw.get('key', ''),
        'id': raw.get('id', ''),
        'summary': fields.get('summary', ''),
        'status': {'name': status.get('name', ''), 'categoryKey': (status.get('statusCategory') or {}).get('key', '')},
        'issuetype': {'name': issuetype.get('name', ''), 'iconUrl': issuetype.get('iconUrl', '')},
        'priority': {'name': pri_name, 'iconUrl': priority.get('iconUrl', ''), 'id': priority.get('id', '')},
        'resolution': {'name': resolution.get('name', '')} if resolution else None,
        'assignee': {
            'displayName': assignee.get('displayName', ''),
            'name': assignee.get('name', ''),
            'avatarUrls': assignee.get('avatarUrls', {}),
        } if assignee else None,
        'reporter': {
            'displayName': reporter.get('displayName', ''),
            'name': reporter.get('name', ''),
            'avatarUrls': reporter.get('avatarUrls', {}),
        } if reporter else None,
        'created': fields.get('created', ''),
        'updated': fields.get('updated', ''),
        'resolutiondate': fields.get('resolutiondate'),
        'description_html': desc_html,
        'labels': fields.get('labels') or [],
        'components': [{'name': c.get('name', '')} for c in (fields.get('components') or [])],
        'fixVersions': [{'name': v.get('name', '')} for v in (fields.get('fixVersions') or [])],
        'versions': [{'name': v.get('name', '')} for v in (fields.get('versions') or [])],
        'issuelinks': issuelinks,
        'attachment': attachments,
        'comment': {'comments': comments},
        'customfield_10130': fields.get('customfield_10130'),
        'customfield_12780': epic_key,
        'customfield_19700': team,
        'sprint': sprint_info,
        'closedSprints': closed_sprints,
        'dev': dev_info,
        'transitions': transitions,
    }
    return 200, result


# ── Team config ──────────────────────────────────────────────────────────────

_DEFAULT_CONFIG = {
    'project_key': '',
    'board_id': 0,
    'board_url': '',
    'board_name': '',
    'team_name': '',
    'team': [],
}

_TEAM_CONFIG_PATH = os.path.join(SCRIPT_DIR, 'team-config.json')
_team_config_cache = None


def load_team_config() -> dict:
    """Read team-config.json, falling back to defaults if missing."""
    global _team_config_cache
    if _team_config_cache is not None:
        return _team_config_cache
    try:
        with open(_TEAM_CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
        # Merge with defaults so new keys are always present
        merged = {**_DEFAULT_CONFIG, **cfg}
        _team_config_cache = merged
    except Exception:
        _team_config_cache = dict(_DEFAULT_CONFIG)
    return _team_config_cache


def save_team_config(cfg: dict) -> None:
    """Write team-config.json and update cache."""
    global _team_config_cache
    with open(_TEAM_CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)
    _team_config_cache = cfg


def get_board_id() -> int:
    return load_team_config().get('board_id', 0)


def get_board_name() -> str:
    """Return configured board name, auto-migrating from Jira if missing."""
    cfg = load_team_config()
    name = cfg.get('board_name', '')
    if not name and cfg.get('board_id'):
        try:
            req = urllib.request.Request(
                f'{JIRA_URL}/rest/agile/1.0/board/{cfg["board_id"]}',
                headers={'Authorization': f'Bearer {JIRA_TOKEN}'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                name = data.get('name', '')
                if name:
                    cfg['board_name'] = name
                    save_team_config(cfg)
                    print(f'  > Auto-detected board name: {name}')
        except Exception as e:
            print(f'  x Failed to fetch board name: {e}')
    return name


def get_team() -> list[str]:
    return load_team_config().get('team', [])


def invalidate_team_config_cache() -> None:
    """Force next load_team_config() to re-read from disk."""
    global _team_config_cache
    _team_config_cache = None


# ── Sprint info helpers ───────────────────────────────────────────────────────

def get_future_sprint_info(board_id: int) -> dict:
    """Fetch ALL future sprints from Jira Agile API.
    Returns a dict mapping sprint_id (str) to sprint info dict.
    Includes all future sprints (with or without startDate)."""
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/agile/1.0/board/{board_id}/sprint?state=future',
        method='GET',
        headers={'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            sprints = json.loads(resp.read().decode('utf-8')).get('values', [])
    except Exception as e:
        print(f'  x Failed to fetch future sprints for board {board_id}: {e}')
        return {}

    result = {}
    for s in sprints:
        result[str(s['id'])] = {
            'id': s['id'],
            'name': s.get('name', ''),
            'state': s.get('state', 'future'),
            'startDate': s.get('startDate', ''),
            'endDate': s.get('endDate', ''),
        }
    return result


def get_active_sprints(board_id: int) -> dict:
    """Fetch active sprints for a board.
    Returns a dict mapping sprint_id (str) to sprint info dict."""
    if not board_id:
        return {}
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/agile/1.0/board/{board_id}/sprint?state=active',
        method='GET',
        headers={'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            sprints = json.loads(resp.read().decode('utf-8')).get('values', [])
        result = {}
        for s in sprints:
            result[str(s['id'])] = {
                'id': s['id'],
                'name': s.get('name', ''),
                'state': s.get('state', 'active'),
                'startDate': s.get('startDate', ''),
                'endDate': s.get('endDate', ''),
            }
        return result
    except Exception as e:
        print(f'  x Failed to fetch active sprints: {e}')
        return {}


def get_board_backlog_count(board_id: int) -> int:
    """Check if a board has issues in the backlog (not in any sprint).
    Returns the count of backlog issues, or 0 on error."""
    req = urllib.request.Request(
        f'{JIRA_URL}/rest/agile/1.0/board/{board_id}/backlog?maxResults=0',
        method='GET',
        headers={'Authorization': f'Bearer {JIRA_TOKEN}'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode('utf-8'))
        return body.get('total', 0)
    except Exception:
        return 0


def get_backlog_issues(board_id: int) -> list[dict]:
    """Fetch issues from the board backlog (not assigned to any sprint)."""
    all_issues = []
    start = 0
    while True:
        req = urllib.request.Request(
            f'{JIRA_URL}/rest/agile/1.0/board/{board_id}/backlog?startAt={start}&maxResults=100'
            f'&fields=customfield_10130,customfield_12780,customfield_19700,assignee,reporter,summary,issuetype,status,priority,updated,created',
            method='GET',
            headers={'Authorization': f'Bearer {JIRA_TOKEN}'}
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            print(f'  x Failed to fetch board backlog: {e}')
            break

        for i in body.get('issues', []):
            fields = i.get('fields', {})
            assignee = fields.get('assignee') or {}
            reporter = fields.get('reporter') or {}
            pri = fields.get('priority') or {}
            pri_name = _strip_pri_name(pri.get('name', ''))
            epic_raw = fields.get('customfield_12780')
            epic_key = ''
            if isinstance(epic_raw, str):
                epic_key = epic_raw
            elif isinstance(epic_raw, dict):
                epic_key = epic_raw.get('value', '') or epic_raw.get('key', '')
            team_raw = fields.get('customfield_19700') or []
            team_values = []
            for opt in (team_raw if isinstance(team_raw, list) else []):
                v = opt.get('value', '') if isinstance(opt, dict) else str(opt)
                if v:
                    team_values.append(v)
            all_issues.append({
                'key':              i['key'],
                'summary':          fields.get('summary', ''),
                'sp':               fields.get('customfield_10130'),
                'assignee_display': assignee.get('displayName') or '',
                'assignee_name':    assignee.get('name') or '',
                'reporter_display': reporter.get('displayName') or '',
                'type':             (fields.get('issuetype') or {}).get('name', 'Story'),
                'status':           (fields.get('status')    or {}).get('name', ''),
                'priority':         pri_name,
                'priority_id':      pri.get('id', ''),
                'priority_icon':    pri.get('iconUrl', ''),
                'sprint_id':        0,
                'epic_key':         epic_key,
                'epic_name':        '',
                'updated':          fields.get('updated', ''),
                'created':          fields.get('created', ''),
                'team':             team_values,
            })

        total = body.get('total', 0)
        start += len(body.get('issues', []))
        if start >= total:
            break

    # Batch-resolve epic names
    epic_keys = list(set(r['epic_key'] for r in all_issues if r['epic_key']))
    if epic_keys:
        epic_names = _resolve_epic_names(epic_keys)
        for r in all_issues:
            if r['epic_key']:
                r['epic_name'] = epic_names.get(r['epic_key'], '')
    return all_issues


_SPRINT_INFO_CACHE = None
_SPRINT_INFO_TIME = 0

def get_all_sprints_cached(board_id: int) -> dict:
    """Cached wrapper — returns all active + future sprints + board backlog (5-min TTL)."""
    global _SPRINT_INFO_CACHE, _SPRINT_INFO_TIME
    now = time.time()
    if _SPRINT_INFO_CACHE is not None and (now - _SPRINT_INFO_TIME) < 300:
        return _SPRINT_INFO_CACHE
    active = get_active_sprints(board_id)
    future = get_future_sprint_info(board_id)
    result = {**active, **future}
    # Add board backlog as a virtual entry if it has issues
    backlog_count = get_board_backlog_count(board_id)
    if backlog_count > 0:
        result['backlog'] = {
            'id': 'backlog',
            'name': 'Backlog',
            'state': 'backlog',
            'startDate': '',
            'endDate': '',
        }
    _SPRINT_INFO_CACHE = result
    _SPRINT_INFO_TIME = now
    return result


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f'  {self.address_string()} {fmt % args}')

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin',  '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _respond(self, code, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type',   'application/json')
        self.send_header('Content-Length', len(body))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # ── /api/health ──
        if parsed.path == '/api/health':
            qs   = urllib.parse.parse_qs(parsed.query)
            skip = set(qs.get('skip', [''])[0].split(','))

            jira_ok = jira_err = None
            if 'jira' not in skip:
                jira_ok, jira_err = check_jira_health()

            docker_ok = docker_err = None
            if 'docker' not in skip:
                docker_ok, docker_err = check_docker()

            self._respond(200, {
                'server': 'ok',
                'jira':      jira_ok,   'jira_error':      jira_err,
                'docker':    docker_ok, 'docker_error':    docker_err,
                'jira_url':  JIRA_URL,
            })
            return

        # ── /api/config ── return team config (re-reads file for freshness)
        if parsed.path == '/api/config':
            invalidate_team_config_cache()
            self._respond(200, load_team_config())
            return

        # ── /api/boards ── search scrum boards by project + name
        if parsed.path == '/api/boards':
            qs = urllib.parse.parse_qs(parsed.query)
            name_filter = qs.get('name', [''])[0].strip()
            project_filter = qs.get('project', [''])[0].strip()
            try:
                params = 'type=scrum&maxResults=50'
                if project_filter:
                    params += '&projectKeyOrId=' + urllib.parse.quote(project_filter)
                if name_filter:
                    params += '&name=' + urllib.parse.quote(name_filter)
                url = f'{JIRA_URL}/rest/agile/1.0/board?{params}'
                req = urllib.request.Request(url, method='GET',
                                             headers={'Authorization': f'Bearer {JIRA_TOKEN}'})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                boards = [{'id': b['id'], 'name': b['name']} for b in data.get('values', [])]
                boards.sort(key=lambda b: b['name'].lower())
                print(f'  > Listed {len(boards)} scrum boards (project={project_filter!r}, name={name_filter!r})')
                self._respond(200, boards)
            except Exception as e:
                print(f'  x Failed to list boards: {e}')
                self._respond(502, {'error': str(e)})
            return

        # ── /api/project-teams ── list team names from Jira Team field for a project
        if parsed.path == '/api/project-teams':
            qs = urllib.parse.parse_qs(parsed.query)
            project = qs.get('project', [''])[0].strip()
            if not project:
                self._respond(200, [])
                return
            teams = []
            hdrs = {'Authorization': f'Bearer {JIRA_TOKEN}'}
            try:
                params = urllib.parse.urlencode({
                    'jql': f'project = "{project}" AND cf[19700] is not EMPTY ORDER BY created DESC',
                    'fields': TEAM_FIELD_ID,
                    'maxResults': 500,
                })
                with urllib.request.urlopen(
                    urllib.request.Request(f'{JIRA_URL}/rest/api/2/search?{params}',
                                           headers=hdrs),
                    timeout=15
                ) as r:
                    s_data = json.loads(r.read().decode('utf-8'))
                seen = set()
                for issue in s_data.get('issues', []):
                    raw = issue.get('fields', {}).get(TEAM_FIELD_ID) or []
                    # Jira Server returns multiselect as list of option dicts: [{id, value, self}]
                    for opt in (raw if isinstance(raw, list) else []):
                        v = opt.get('value', '') if isinstance(opt, dict) else str(opt)
                        if v and v not in seen:
                            seen.add(v)
                            teams.append(v)
            except Exception as e:
                print(f'  x Failed to list teams via JQL: {e}')
            teams = sorted(teams, key=str.lower)
            print(f'  > Listed {len(teams)} teams for project {project!r}')
            self._respond(200, teams)
            return

        # ── /api/board-sprints ── return ALL sprints (active + future) for a board
        if parsed.path == '/api/board-sprints':
            qs = urllib.parse.parse_qs(parsed.query)
            bid = int(qs.get('board_id', ['0'])[0])
            if not bid:
                self._respond(400, {'error': 'Missing board_id'})
                return
            try:
                active = get_active_sprints(bid)
                future = get_future_sprint_info(bid)
                all_sprints = {**active, **future}
                # Check for board backlog
                backlog_count = get_board_backlog_count(bid)
                if backlog_count > 0:
                    all_sprints['backlog'] = {'id': 'backlog', 'name': 'Backlog', 'state': 'backlog'}
                # Flatten to {id_str: name} for the frontend
                backlog_sprints = {k: v['name'] for k, v in all_sprints.items()}
                self._respond(200, {'backlog_sprints': backlog_sprints})
            except Exception as e:
                self._respond(502, {'error': str(e)})
            return

        # ── /api/sprint-info ── simplified board info + all sprints
        if parsed.path == '/api/sprint-info':
            cfg = load_team_config()
            bid = cfg.get('board_id', 0)
            if not bid:
                self._respond(404, {'error': 'No board configured',
                                    'hint': 'Configure a board in Settings'})
                return
            all_sprints = get_all_sprints_cached(bid)
            # Flatten to {id_str: name} for the frontend
            backlog_sprints = {k: v['name'] for k, v in all_sprints.items()}
            self._respond(200, {
                'team': cfg.get('team', []),
                'board_name': cfg.get('board_name', ''),
                'board_id': bid,
                'project_key': cfg.get('project_key', ''),
                'team_name': cfg.get('team_name', ''),
                'backlog_sprints': backlog_sprints,
            })
            return

        # ── /api/sprint-issues ──
        if parsed.path == '/api/sprint-issues':
            qs           = urllib.parse.parse_qs(parsed.query)
            sprints_param = qs.get('sprints', [''])[0]
            sprint_tokens = [s.strip() for s in sprints_param.split(',') if s.strip()]
            issues_by_sprint = {}
            for token in sprint_tokens:
                if token == 'backlog':
                    # Fetch board backlog (issues not in any sprint)
                    bid = get_board_id()
                    if bid:
                        issues = get_backlog_issues(bid)
                        print(f'  > Board backlog: {len(issues)} issues')
                        issues_by_sprint['backlog'] = issues
                elif token.lstrip('-').isdigit():
                    sid = int(token)
                    issues = get_issues_for_sprint(sid)
                    print(f'  > Sprint {sid}: {len(issues)} issues')
                    issues_by_sprint[str(sid)] = issues
            # Include Jira priorities (fetched + cached on first call)
            priorities = fetch_jira_priorities()
            self._respond(200, {'sprints': issues_by_sprint, 'priorities': priorities})
            return

        # ── /api/epic-children ──
        if parsed.path == '/api/epic-children':
            qs = urllib.parse.parse_qs(parsed.query)
            key = qs.get('key', [''])[0].strip()
            if not key:
                self._respond(400, {'error': 'Missing key parameter'})
                return
            children = get_epic_children(key)
            print(f'  > Epic {key}: {len(children)} children')
            self._respond(200, {'children': children})
            return

        # ── /api/backlog-prefs ── read saved backlog selections
        if parsed.path == '/api/backlog-prefs':
            path = os.path.join(SCRIPT_DIR, 'backlog-prefs.json')
            try:
                with open(path, 'r') as f:
                    prefs = json.load(f)
            except Exception:
                prefs = {}
            self._respond(200, prefs)
            return

        # ── /api/issue-types ── return known issue types
        if parsed.path == '/api/issue-types':
            path = os.path.join(SCRIPT_DIR, 'issue-types.json')
            try:
                with open(path, 'r') as f:
                    types = json.load(f)
            except Exception:
                types = []
            self._respond(200, types)
            return

        # ── /api/transitions ── get available transitions for an issue
        if parsed.path == '/api/transitions':
            qs = urllib.parse.parse_qs(parsed.query)
            key = qs.get('key', [''])[0].strip()
            if not key:
                self._respond(400, {'error': 'Missing key parameter'})
                return
            status, transitions = get_transitions(key)
            if status == 200:
                print(f'  > Transitions for {key}: {len(transitions)} available')
                self._respond(200, {'transitions': transitions})
            else:
                print(f'  x Failed to get transitions for {key} (HTTP {status})')
                self._respond(502, {'error': f'Jira returned HTTP {status}'})
            return

        # ── /api/issue-detail ── get full issue detail with rendered fields
        if parsed.path == '/api/issue-detail':
            qs = urllib.parse.parse_qs(parsed.query)
            key = qs.get('key', [''])[0].strip()
            if not key:
                self._respond(400, {'error': 'Missing key parameter'})
                return
            status, detail = get_issue_detail(key)
            if status == 200:
                print(f'  > Issue detail for {key}')
                self._respond(200, detail)
            else:
                print(f'  x Failed to get detail for {key} (HTTP {status})')
                self._respond(502, {'error': f'Jira returned HTTP {status}'})
            return

        # ── /icons/* ── serve priority icon files
        if parsed.path.startswith('/icons/'):
            safe = os.path.normpath(parsed.path.lstrip('/'))
            if safe.startswith('icons' + os.sep) or safe.startswith('icons/'):
                fpath = os.path.join(SCRIPT_DIR, safe)
                if os.path.isfile(fpath):
                    ext = os.path.splitext(fpath)[1].lower()
                    ctype = {'.svg': 'image/svg+xml', '.png': 'image/png',
                             '.gif': 'image/gif', '.jpg': 'image/jpeg'}.get(ext, 'application/octet-stream')
                    with open(fpath, 'rb') as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', ctype)
                    self.send_header('Content-Length', len(data))
                    self._cors()
                    self.end_headers()
                    self.wfile.write(data)
                    return
            self._respond(404, {'error': 'Icon not found'})
            return

        self._respond(404, {'error': 'Not found'})

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length)

        # ── /api/config ── save team config
        if self.path == '/api/config':
            try:
                data = json.loads(body)
                cfg = load_team_config()
                # Only update allowed fields
                for key in ('project_key', 'board_id', 'board_url', 'board_name', 'team_name', 'team'):
                    if key in data:
                        cfg[key] = data[key]
                save_team_config(cfg)
                # Invalidate sprint cache since board_id may have changed
                global _SPRINT_INFO_CACHE
                _SPRINT_INFO_CACHE = None
                self._respond(200, {'ok': True})
            except Exception as e:
                self._respond(500, {'ok': False, 'error': str(e)})
            return

        # ── /api/backlog-prefs ── save backlog selections
        if self.path == '/api/backlog-prefs':
            try:
                data = json.loads(body)
                path = os.path.join(SCRIPT_DIR, 'backlog-prefs.json')
                with open(path, 'w') as f:
                    json.dump(data, f, indent=2)
                self._respond(200, {'ok': True})
            except Exception as e:
                self._respond(500, {'ok': False, 'error': str(e)})
            return

        # ── /api/edit ──
        if self.path == '/api/edit':
            try:
                data      = json.loads(body)
                issue_key = data['issue_key']
            except Exception as e:
                self._respond(400, {'error': f'Bad request: {e}'}); return

            fields = {}
            if 'sp' in data:
                sp_val   = data['sp']
                sp_float = float(sp_val) if sp_val is not None else None
                fields['customfield_10130'] = sp_float
                if sp_float is None:
                    # Clearing SP: clear all three
                    fields['timetracking'] = {'originalEstimate': '', 'remainingEstimate': ''}
                else:
                    # SP=0: originalEstimate='4h'; SP>0: derived from SP value
                    orig_est    = '4h' if sp_float == 0 else _sp_to_estimate(sp_float)
                    sp_secs     = 14400 if sp_float == 0 else int(sp_float * 8 * 3600)
                    logged_secs = get_time_spent(issue_key)
                    rem_secs    = max(0, sp_secs - logged_secs)
                    fields['timetracking'] = {
                        'originalEstimate': orig_est,
                        'remainingEstimate': _secs_to_estimate(rem_secs),
                    }
            if 'assignee' in data:
                name = (data['assignee'] or '').strip()
                if name:
                    username, err = find_user_name(name)
                    if err:
                        print(f'  x User lookup failed for "{name}": {err}')
                        self._respond(400, {'error': f'Cannot assign: {err}'}); return
                    print(f'  > Resolved "{name}" -> "{username}"')
                    fields['assignee'] = {'name': username}
                else:
                    fields['assignee'] = None  # unassign
            if 'priority' in data:
                pri_id = data.get('priority_id', '')
                if pri_id:
                    fields['priority'] = {'id': pri_id}
                else:
                    fields['priority'] = {'name': data['priority']}

            if not fields:
                self._respond(400, {'error': 'No fields to update'}); return

            print(f'  > Editing {issue_key}: {list(fields.keys())}')
            status, msg = update_issue_fields(issue_key, fields)
            if status in (200, 201, 204):
                print(f'  ok {issue_key} updated')
                self._respond(200, {'ok': True})
            else:
                print(f'  x {issue_key} edit failed (HTTP {status}): {msg[:120]}')
                self._respond(502, {'ok': False, 'error': msg[:200]})
            return

        # ── /api/move ──
        if self.path == '/api/move':
            try:
                data      = json.loads(body)
                issue_key = data['issue_key']
                raw_sprint = data['sprint_id']
            except Exception as e:
                self._respond(400, {'error': f'Bad request: {e}'})
                return

            if str(raw_sprint) == 'backlog':
                print(f'  > Moving {issue_key} to board backlog ...')
                status, msg = move_issue_to_backlog(issue_key)
                sprint_id = 'backlog'
            else:
                sprint_id = int(raw_sprint)
                print(f'  > Moving {issue_key} to sprint {sprint_id} ...')
                status, msg = move_issue_to_sprint(issue_key, sprint_id)

            if status in (200, 201, 204):
                print(f'  ok {issue_key} moved (HTTP {status})')
                self._respond(200, {'ok': True, 'issue_key': issue_key, 'sprint_id': sprint_id})
            else:
                print(f'  x {issue_key} failed (HTTP {status}): {msg[:120]}')
                self._respond(502, {'ok': False, 'error': msg[:200]})
            return

        # ── /api/transition ── execute a workflow transition
        if self.path == '/api/transition':
            try:
                data          = json.loads(body)
                issue_key       = data['issue_key']
                transition_id   = str(data['transition_id'])
                resolution      = data.get('resolution')
                transition_name = data.get('transition_name', '')
            except Exception as e:
                self._respond(400, {'error': f'Bad request: {e}'})
                return

            print(f'  > Transitioning {issue_key} (transition={transition_id} "{transition_name}", resolution={resolution}) ...')
            status, msg = transition_issue(issue_key, transition_id, resolution, transition_name)

            if status in (200, 201, 204):
                print(f'  ok {issue_key} transitioned (HTTP {status})')
                self._respond(200, {'ok': True, 'issue_key': issue_key})
            else:
                print(f'  x {issue_key} transition failed (HTTP {status}): {msg[:120]}')
                self._respond(502, {'ok': False, 'error': msg[:200]})
            return

        # ── /api/bulk-move ── batch move issues to a sprint
        if self.path == '/api/bulk-move':
            try:
                data      = json.loads(body)
                issues    = data['issues']
                sprint_id = int(data['sprint_id'])
            except Exception as e:
                self._respond(400, {'error': f'Bad request: {e}'})
                return

            print(f'  > Bulk-moving {len(issues)} issues to sprint {sprint_id} ...')
            results = []
            for issue_key in issues:
                status, msg = move_issue_to_sprint(issue_key, sprint_id)
                ok = status in (200, 201, 204)
                results.append({
                    'issue_key': issue_key,
                    'ok': ok,
                    'error': '' if ok else msg[:200],
                })
                if ok:
                    print(f'  ok {issue_key} moved')
                else:
                    print(f'  x {issue_key} failed (HTTP {status})')

            succeeded = sum(1 for r in results if r['ok'])
            print(f'  > Bulk-move complete: {succeeded}/{len(issues)} succeeded')
            self._respond(200, {'ok': True, 'results': results})
            return

        # ── /api/bulk-transition ── batch transition issues
        if self.path == '/api/bulk-transition':
            try:
                data          = json.loads(body)
                issues        = data['issues']
                transition_id = str(data['transition_id'])
                resolution    = data.get('resolution')
            except Exception as e:
                self._respond(400, {'error': f'Bad request: {e}'})
                return

            print(f'  > Bulk-transitioning {len(issues)} issues (transition={transition_id}, resolution={resolution}) ...')
            results = []
            for issue_key in issues:
                status, msg = transition_issue(issue_key, transition_id, resolution)
                ok = status in (200, 201, 204)
                results.append({
                    'issue_key': issue_key,
                    'ok': ok,
                    'error': '' if ok else msg[:200],
                })
                if ok:
                    print(f'  ok {issue_key} transitioned')
                else:
                    print(f'  x {issue_key} failed (HTTP {status})')

            succeeded = sum(1 for r in results if r['ok'])
            print(f'  > Bulk-transition complete: {succeeded}/{len(issues)} succeeded')
            self._respond(200, {'ok': True, 'results': results})
            return

        self._respond(404, {'error': 'Not found'})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    server = HTTPServer(('localhost', PORT), Handler)
    print(f'Backlog Sweeper server running on http://localhost:{PORT}')
    print(f'Jira: {JIRA_URL}')
    print('Open backlog-sweep.html in Chrome.')
    print('Ctrl+C to stop.\n')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
