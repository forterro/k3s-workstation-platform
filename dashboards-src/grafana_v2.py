"""Grafana 13 schema v2 dashboards (TabsLayout) following the 360 Observability dashboard rules.

Shared by the workstation dashboards (k3s-workstation-platform `dashboards-src/`) and the agent harness: keep the two
copies identical. Rules applied (notepad dc-infrastructure-improvement projects/observability-360/development/
dashboards.md): tabs only; the first tab is `Overview` (global health on the left, one clickable stat per tab stacked
one per line, health history below); a navigation bar on every tab; tables never side by side, columns auto except
descriptions (fill + wrap); time series legends on the right (table, max / last); at most 4 stats per row; the main
list of a tab is last and grows to the bottom of the screen; every tab shows something over time; empty-but-healthy
panels say so; colours red = broken, orange = to handle, green = fine, grey = not measured, blue = information.
"""
import json
import re

# Navigation arrows of the tab bars (typographic quotes, on purpose).
PREV, NEXT = "\u2039", "\u203a"

DS = {"type": "prometheus", "uid": "${datasource}"}
PALETTE = {"green": "#78C282", "orange": "#F4A54D", "red": "#EC6A73", "blue": "#6E9FE6", "grey": "#868A8E",
           "purple": "#B68DD8", "yellow": "#E8CF6A"}
EMPTY_TILE = "#2F333A"
OVERVIEW_ROW = 3
NAV_H = 2
TAB_FILL_HEIGHT = 22
PX_PER_COLUMN = 40
AUTO_COLUMN_PX = 96
TIME_PANELS = ("timeseries", "state-timeline")
STATUS_MAP = [{"type": "value", "options": {
    "0": {"text": "OK", "color": "green", "index": 0},
    "1": {"text": "DEGRADED", "color": "orange", "index": 1},
    "2": {"text": "FAILED", "color": "red", "index": 2}}}]


def steps(*pairs):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in pairs]}


STATE = steps(("grey", None), ("green", 0), ("orange", 1), ("red", 2))
INFO = steps(("blue", None))
COUNT_BAD = steps(("green", None), ("red", 1))
COUNT_TODO = steps(("green", None), ("orange", 1))
PERCENT_USED = steps(("green", None), ("orange", 70), ("red", 90))
RATIO_USED = steps(("green", None), ("orange", 0.6), ("red", 0.8))


def tab_slug(title):
    """URL slug of a tab (matched by ?dtab=)."""
    return re.sub(r" +", "-", re.sub(r"[^\w ]+", "", title.lower())).strip("-")


def target(expr, ref="A", instant=True, fmt="table", legend=None):
    t = {"expr": expr, "refId": ref, "instant": instant, "range": not instant, "format": fmt}
    if legend:
        t["legendFormat"] = legend
    return t


def range_target(expr, legend, ref="A"):
    return target(expr, ref=ref, instant=False, fmt="time_series", legend=legend)


def _defaults(unit, thresholds, mappings, decimals, no_value, link):
    d = {"unit": unit, "noValue": no_value, "color": {"mode": "thresholds"}, "thresholds": thresholds or INFO,
         "mappings": mappings or []}
    if decimals is not None:
        d["decimals"] = decimals
    if link:
        d["links"] = [{"title": "Open", "url": link}]
    return d


def stat(title, desc, expr, thresholds=None, unit="none", decimals=None, no_value="not measured", mappings=None,
         link=None, legend=None, text_mode="value"):
    return {"type": "stat", "title": title, "description": desc,
            "targets": [target(expr, fmt="time_series", legend=legend)],
            "fieldConfig": {"defaults": _defaults(unit, thresholds, mappings, decimals, no_value, link),
                            "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "background_solid", "graphMode": "none", "justifyMode": "center",
                        "textMode": text_mode, "wideLayout": True, "orientation": "auto",
                        "showPercentChange": False}}


def tiles(title, desc, exprs, link=None, unit="none"):
    """One tile per (expr, name, colour): the tile is coloured when its value is above 0, dark grey at 0.
    link may use ${__field.displayName}."""
    p = stat(title, desc, exprs[0][0], unit=unit, link=link, no_value="0", text_mode="value_and_name")
    p["targets"] = [target(e, ref=chr(65 + i), fmt="time_series", legend=n) for i, (e, n, _) in enumerate(exprs)]
    p["fieldConfig"]["overrides"] = [
        {"matcher": {"id": "byName", "options": n},
         "properties": [{"id": "thresholds", "value": steps((EMPTY_TILE, None), (c, 0.5))}]} for _, n, c in exprs]
    p["options"]["text"] = {"titleSize": 12, "valueSize": 14}
    p["options"].update(minVizWidth=96, minVizHeight=40)
    return p


def col(name, unit=None, thresholds=None, mappings=None, decimals=None, link=None, fill=False, hidden=False,
        no_value=None, width=None):
    """Column override. Columns are auto (equal share of the width); only a description column `fill`s and wraps.
    width: identifier measured cut by the UI check, px = longest text measured + ~16 (cite it in a comment)."""
    props = [{"id": "custom.minWidth", "value": 8 * len(name) + 40}]
    if width:
        props.append({"id": "custom.width", "value": width})
    if fill:
        props += [{"id": "custom.width", "value": "fill"},
                  {"id": "custom.cellOptions", "value": {"type": "auto", "wrapText": True}}]
    if thresholds:
        props += [{"id": "thresholds", "value": thresholds}, {"id": "color", "value": {"mode": "thresholds"}},
                  {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}}]
    if mappings:
        props.append({"id": "mappings", "value": mappings})
    if unit:
        props.append({"id": "unit", "value": unit})
    if decimals is not None:
        props.append({"id": "decimals", "value": decimals})
    if link:
        props.append({"id": "links", "value": [{"title": "Open", "url": link}]})
    if no_value is not None:
        props.append({"id": "noValue", "value": no_value})
    if hidden:
        props.append({"id": "custom.hidden", "value": True})
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


def _resolve_fill(panel, w):
    """Give the `fill` columns of a table the width its other columns leave in a panel of w grid columns at 1080 px
    (Grafana has no fill width: an unresolved one collapses the column)."""
    props = {o["matcher"]["options"]: {q["id"]: q["value"] for q in o["properties"]}
             for o in panel["fieldConfig"]["overrides"]}
    shown = next(t for t in panel["transformations"] if t["id"] == "filterFieldsByName")["options"]["include"]["names"]
    fill = [c for c in shown if props.get(c, {}).get("custom.width") == "fill"]
    if not fill:
        return
    others = [c for c in shown if c not in fill]
    fixed = sum(v for c in others if isinstance(v := props.get(c, {}).get("custom.width"), int))
    auto = [c for c in others if not isinstance(props.get(c, {}).get("custom.width"), int)]
    share = max([AUTO_COLUMN_PX] + [props.get(c, {}).get("custom.minWidth", AUTO_COLUMN_PX) for c in auto])
    room = (w * PX_PER_COLUMN - fixed - len(auto) * share) // len(fill)
    for o in panel["fieldConfig"]["overrides"]:
        if o["matcher"]["options"] in fill:
            for q in o["properties"]:
                if q["id"] == "custom.width":
                    q["value"] = max(room, props[o["matcher"]["options"]]["custom.minWidth"])


def table(title, desc, exprs, rename, overrides=(), sort=None, no_value="-"):
    """exprs: instant queries merged on their common labels; rename: ordered {field: column} (only these are kept)."""
    targets = [target(e, ref=chr(65 + i)) for i, e in enumerate(exprs)]
    tr = ([{"id": "organize", "options": {"excludeByName": {"__name__": True}}}, {"id": "merge", "options": {}}]
          if len(targets) > 1 else [])
    tr += [{"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": rename,
                                          "indexByName": {k: i for i, k in enumerate(rename)}}},
           {"id": "filterFieldsByName", "options": {"include": {"names": list(rename.values())}}}]
    # Every column gets its header minimum: without one Grafana keeps 150 px, which scrolls busy tables.
    named = {o["matcher"]["options"] for o in overrides}
    overrides = list(overrides) + [col(c) for c in rename.values() if c not in named]
    p = {"type": "table", "title": title, "description": desc, "targets": targets, "transformations": tr,
         "fieldConfig": {"defaults": {"noValue": no_value,
                                      "custom": {"align": "auto", "filterable": True, "cellOptions": {"type": "auto"}}},
                         "overrides": list(overrides)},
         "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}}}
    if sort:
        p["options"]["sortBy"] = [{"displayName": sort[0], "desc": sort[1]}]
    return p


def timeline(title, desc, expr, legend, thresholds=STATE, mappings=STATUS_MAP, link=None, rows=10):
    d = {"color": {"mode": "thresholds"}, "thresholds": thresholds, "mappings": mappings,
         "custom": {"fillOpacity": 80, "lineWidth": 0}}
    if link:
        d["links"] = [{"title": "Open", "url": link}]
    return {"type": "state-timeline", "title": title, "description": desc, "rows": rows,
            "targets": [range_target(expr, legend)], "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"showValue": "never", "mergeValues": True, "alignValue": "left", "rowHeight": 0.8,
                        "legend": {"showLegend": False}, "tooltip": {"mode": "single"}}}


def table_height(rows):
    """Grid height showing `rows` table rows without a scroll bar (small row height, header, title)."""
    return -(-(rows * 33 + 80) // 38)


def bars_height(rows):
    return -(-(rows * 26 + 50) // 38)


def timeline_height(rows):
    return max(4, -(-(rows * 24 + 64) // 38))


def series(title, desc, targets, unit="none", stack=False, calcs=("max", "lastNotNull"), no_value=None, min0=True,
           health=False):
    d = {"unit": unit, "color": {"mode": "palette-classic"},
         "custom": {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 10 if not stack else 60,
                    "showPoints": "never", "stacking": {"mode": "normal" if stack else "none"}}}
    if min0:
        d["min"] = 0
    if no_value:
        d["noValue"] = no_value
    if health:
        d.update(min=0, max=2, decimals=0, mappings=STATUS_MAP)
        d["custom"]["lineInterpolation"] = "stepAfter"
    return {"type": "timeseries", "title": title, "description": desc, "targets": targets,
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": list(calcs)},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def bars(title, desc, expr, legend, unit="none", thresholds=None, max_=None, no_value=None):
    d = {"unit": unit, "min": 0, "color": {"mode": "thresholds"}, "thresholds": thresholds or INFO}
    if max_ is not None:
        d["max"] = max_
    if no_value:
        d["noValue"] = no_value
    return {"type": "bargauge", "title": title, "description": desc,
            "targets": [target(expr, fmt="time_series", legend=legend)],
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"displayMode": "basic", "orientation": "horizontal", "showUnfilled": True,
                        "valueMode": "color", "namePlacement": "left", "sizing": "auto", "minVizHeight": 14,
                        "maxVizHeight": 22, "reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                                                              "values": False}}}


def text(content, title=""):
    return {"type": "text", "title": title, "options": {"mode": "markdown", "content": content}}


# ---------------------------------------------------------------- variables
def v_datasource(default="prometheus", regex=""):
    return {"name": "datasource", "type": "datasource", "label": "Data source", "query": "prometheus",
            "regex": regex, "current": {"text": default, "value": default}}


def qvar(name, label, query, multi=True, include_all=True, hide=0):
    return {"name": name, "type": "query", "label": label, "query": {"query": query, "refId": name},
            "definition": query, "multi": multi, "includeAll": include_all, "allValue": ".*" if include_all else None,
            "hide": hide, "refresh": 2,
            "current": ({"text": ["All"], "value": ["$__all"]} if multi else {"text": "All", "value": "$__all"})
            if include_all else None}


def cvar(name, label, choices, hide=2, all_value=".*"):
    """Hidden filter variable of a tab (set by its tiles): All, then each choice."""
    return {"name": name, "type": "custom", "label": label, "hide": hide, "allValue": all_value,
            "query": ",".join(choices), "options": [], "includeAll": True, "multi": False,
            "current": {"text": "All", "value": "$__all"}}


HIDE = {0: "dontHide", 1: "hideLabel", 2: "hideVariable"}


def _v2_variable(v):
    common = {"name": v["name"], "label": v.get("label", ""), "hide": HIDE[v.get("hide", 0)],
              "skipUrlSync": v.get("skip_url", False)}
    if v["type"] == "datasource":
        return {"kind": "DatasourceVariable", "spec": {
            **common, "pluginId": v["query"], "refresh": "onDashboardLoad", "regex": v.get("regex", ""),
            "current": v.get("current") or {}, "options": [], "multi": False, "includeAll": False,
            "allowCustomValue": True}}
    if v["type"] == "custom":
        return {"kind": "CustomVariable", "spec": {
            **common, "query": v["query"], "current": v["current"], "options": v["options"], "multi": False,
            "includeAll": True, "allValue": v["allValue"], "allowCustomValue": False}}
    spec = {**common, "current": v.get("current") or {"text": "", "value": ""}, "refresh": "onTimeRangeChanged",
            "query": {"kind": "DataQuery", "group": "prometheus", "version": "v0",
                      "datasource": {"name": "${datasource}"}, "spec": v["query"]},
            "regex": "", "sort": "alphabeticalAsc", "definition": v["definition"], "options": [],
            "multi": v["multi"], "includeAll": v["includeAll"], "allowCustomValue": True}
    if v["includeAll"] and v.get("allValue") is not None:
        spec["allValue"] = v["allValue"]
    return {"kind": "QueryVariable", "spec": spec}


def _palette(node):
    if isinstance(node, dict):
        return {k: (PALETTE.get(v, v) if k in ("color", "fixedColor") and isinstance(v, str) else _palette(v))
                for k, v in node.items()}
    if isinstance(node, list):
        return [_palette(v) for v in node]
    return node


def _v2_panel(p):
    queries = [{"kind": "PanelQuery", "spec": {"refId": t["refId"], "hidden": False, "query": {
        "kind": "DataQuery", "group": "prometheus", "version": "v0", "datasource": {"name": "${datasource}"},
        "spec": {k: t[k] for k in ("expr", "format", "instant", "range", "legendFormat") if k in t}}}}
        for t in p.get("targets", [])]
    return {"kind": "Panel", "spec": {
        "id": p["id"], "title": p.get("title", ""), "description": p.get("description", ""), "links": [],
        "transparent": bool(p.get("transparent")),
        "data": {"kind": "QueryGroup", "spec": {
            "queries": queries, "queryOptions": {},
            "transformations": [{"kind": t["id"], "spec": t} for t in p.get("transformations", [])]}},
        "vizConfig": {"kind": "VizConfig", "group": p["type"], "version": "", "spec": {
            "options": p.get("options", {}), "fieldConfig": p.get("fieldConfig", {"defaults": {}, "overrides": []})}}}}


class Dash:
    """A dashboard of tabs. section() starts a tab; line() / stats() add panels top-down. health() makes the first tab
    the generated Overview: global health, then the `summary` stat of each tab (clickable), health history below."""

    def __init__(self, uid, title, description, variables, tags=(), crumbs=(), kind="Overview", subject=("", ""),
                 links=(), time="now-24h"):
        self.uid, self.title, self.description = uid, title, description
        self.variables, self.tags, self.crumbs = variables, list(tags), list(crumbs)
        self.kind, self.subject, self.extra_links, self.time = kind, subject, list(links), time
        self.sections, self._id, self.global_health = [], 0, None

    def health(self, expr, desc):
        self.global_health = (expr, desc)

    def section(self, title, summary=None, health=None):
        """summary: panel shown on the Overview tab for this tab (linked to it); health: state expression of the tab
        objects (0 OK, 1 DEGRADED, 2 FAILED), a line of the health history."""
        if title == "Overview" or any(s["title"] == title for s in self.sections):
            raise ValueError(f"{self.uid}: duplicate tab {title}")
        if summary:
            summary["fieldConfig"]["defaults"]["links"] = [{"title": f"Open {title}", "url": self.tab_url(title)}]
        self.sections.append({"title": title, "items": [], "y": 0, "summary": summary, "health": health})

    def tab_url(self, title, extra=""):
        """Same dashboard, tab `title`, selection variables and time range kept (tab filters reset unless set)."""
        keep = [f"${{{v['name']}:queryparam}}" for v in self.variables if v.get("hide", 0) != 2]
        return f"/d/{self.uid}?dtab={tab_slug(title)}{extra}&" + "&".join([*keep, "${__url_time_range}"])

    def line(self, h, *panels_widths):
        sec, x = self.sections[-1], 0
        h = max([h] + [timeline_height(p["rows"]) for p, _ in panels_widths if p["type"] == "state-timeline"])
        for panel, w in panels_widths:
            self._id += 1
            panel["id"] = self._id
            if panel["type"] == "table":
                _resolve_fill(panel, w)
            sec["items"].append((panel, x, sec["y"], w, h))
            x += w
        if x > 24:
            raise ValueError(f"{self.uid}: line wider than 24")
        sec["y"] += h

    def stats(self, h, *panels, per_row=4):
        w = 24 // per_row
        for i in range(0, len(panels), per_row):
            self.line(h, *((p, w) for p in panels[i:i + per_row]))

    # ------------------------------------------------------------ render
    def _overview(self):
        expr, desc = self.global_health
        sec = {"title": "Overview", "items": [], "y": 0}
        summaries = [s["summary"] for s in self.sections if s["summary"]]
        history = [range_target(expr, "Health", "A")] + [
            range_target(s["health"], s["title"], chr(66 + i)) for i, s in enumerate(
                [s for s in self.sections if s["health"]])]
        rows = max(len(summaries), 1)
        self._id += 1
        h = stat("Health", desc, expr, STATE, mappings=STATUS_MAP)
        h["id"] = self._id
        sec["items"].append((h, 0, 0, 4, OVERVIEW_ROW * rows))
        for n, p in enumerate(summaries):
            self._id += 1
            p["id"] = self._id
            sec["items"].append((p, 4, n * OVERVIEW_ROW, 20, OVERVIEW_ROW))
        hist = series("Health history", "Global health and health of every tab (0 OK, 1 DEGRADED, 2 FAILED).",
                      history, health=True)
        self._id += 1
        hist["id"] = self._id
        sec["items"].append((hist, 0, OVERVIEW_ROW * rows, 24, 8))
        return sec

    def _nav(self, secs):
        here = f"/d/{self.uid}?" + "&".join(
            [f"${{{v['name']}:queryparam}}" for v in self.variables if v.get("hide", 0) != 2] + ["${__url_time_range}"])
        crumbs = [f"[{t}]({u})" for t, u in self.crumbs] + [f"[{self.title}]({here}&dtab=overview)"]
        for i, sec in enumerate(secs):
            left = []
            if i:
                prev = secs[i - 1]["title"]
                left.append(f'[«]({here}&dtab=overview "Overview tab")')
                left.append(f'[{PREV} {prev}]({here}&dtab={tab_slug(prev)} "Previous tab")')
            right = (f' &nbsp;&nbsp; [{secs[i + 1]["title"]} {NEXT}]({here}&dtab={tab_slug(secs[i + 1]["title"])} '
                     f'"Next tab")' if i + 1 < len(secs) else "")
            otype, oname = self.subject
            md = ("&nbsp; ".join(left) + (" &nbsp;&nbsp; " if left else "") + f" {NEXT} ".join(
                [*crumbs, f"**{sec['title']}**"]) + right
                + f"<br>{self.kind} &nbsp;·&nbsp; {otype} &nbsp;·&nbsp; **{oname}**")
            self._id += 1
            bar = {"type": "text", "title": "", "id": self._id, "transparent": True, "nav": True,
                   "options": {"mode": "markdown", "content": md}}
            sec["items"] = [(bar, 0, 0, 24, NAV_H)] + [(p, x, y + NAV_H, w, h) for p, x, y, w, h in sec["items"]]

    @staticmethod
    def _filled(sec):
        """The last table of a tab (its main list) grows to the bottom of the screen."""
        items = list(sec["items"])
        bottom = max((y + h for _, _, y, _, h in items), default=0)
        tables = [i for i, it in enumerate(items) if it[0]["type"] == "table"]
        if bottom >= TAB_FILL_HEIGHT or not tables:
            return items
        grow = max(tables, key=lambda i: items[i][2])
        delta, top = TAB_FILL_HEIGHT - bottom, items[grow][2]
        return [(p, x, y + delta if y > top else y, w, h + delta if i == grow else h)
                for i, (p, x, y, w, h) in enumerate(items)]

    def sections_rendered(self):
        if not hasattr(self, "_secs"):
            self._secs = ([self._overview()] if self.global_health else []) + self.sections
            self._nav(self._secs)
        return self._secs

    def render(self):
        elements, tabs = {}, []
        for sec in self.sections_rendered():
            items = []
            for panel, x, y, w, h in self._filled(sec):
                name = f"panel-{panel['id']}"
                elements[name] = _palette(_v2_panel(panel))
                items.append({"kind": "GridLayoutItem", "spec": {"x": x, "y": y, "width": w, "height": h,
                                                                  "element": {"kind": "ElementReference",
                                                                              "name": name}}})
            tabs.append({"kind": "TabsLayoutTab", "spec": {"title": sec["title"],
                                                            "layout": {"kind": "GridLayout",
                                                                       "spec": {"items": items}}}})
        links = [{"title": "Overview tab", "type": "link", "icon": "dashboard", "url": f"/d/{self.uid}?dtab=overview",
                  "keepTime": True, "includeVars": True, "targetBlank": False}, *self.extra_links]
        return {
            "apiVersion": "dashboard.grafana.app/v2beta1", "kind": "Dashboard", "metadata": {"name": self.uid},
            "spec": {
                "title": self.title, "description": self.description, "tags": self.tags, "editable": True,
                "cursorSync": "Crosshair", "liveNow": False, "preload": False,
                "timeSettings": {"timezone": "browser", "from": self.time, "to": "now", "autoRefresh": "1m",
                                 "autoRefreshIntervals": ["30s", "1m", "5m", "15m", "30m", "1h"],
                                 "hideTimepicker": False, "fiscalYearStartMonth": 0},
                "links": [{"tooltip": "", "tags": [], "asDropdown": False, **lk} for lk in links],
                "annotations": [], "variables": [_v2_variable(v) for v in self.variables],
                "elements": elements, "layout": {"kind": "TabsLayout", "spec": {"tabs": tabs}}}}

    # ------------------------------------------------------------ gates
    def check(self):
        """Rule violations of this dashboard (empty list = compliant)."""
        errs = []
        secs = self.sections_rendered()
        if not self.global_health or secs[0]["title"] != "Overview":
            errs.append("the first tab must be the generated Overview tab (call health())")
        if len(self.uid) > 40:
            errs.append(f"uid longer than 40: {self.uid}")
        for sec in secs:
            items = sec["items"]
            if not any(p["type"] in TIME_PANELS for p, *_ in items):
                errs.append(f"tab {sec['title']}: nothing over time (timeline or time series)")
            rows = {}
            for p, _x, y, w, _h in items:
                rows.setdefault(y, []).append((p, w))
                if p["type"] == "timeseries" and p["options"]["legend"].get("placement") != "right":
                    errs.append(f"{sec['title']} / {p['title']}: legend not on the right")
                overview_health = sec["title"] == "Overview" and p["title"] == "Health"
                if p["type"] == "stat" and w < 5 and not overview_health:
                    errs.append(f"{sec['title']} / {p['title']}: stat narrower than 5 columns")
                # Measured at 1080 px: about 10 px per character, plus the padding and the description icon.
                if p["type"] == "stat" and len(p["title"]) * 10 + 56 > w * PX_PER_COLUMN:
                    errs.append(f"{sec['title']} / {p['title']}: title too long for {w} columns")
                if p["type"] == "table":
                    shown = next(t for t in p["transformations"] if t["id"] == "filterFieldsByName")[
                        "options"]["include"]["names"]
                    props = {o["matcher"]["options"]: {q["id"]: q["value"] for q in o["properties"]}
                             for o in p["fieldConfig"]["overrides"]}
                    fixed = {c: props.get(c, {}).get("custom.width") for c in shown}
                    fixed = {c: v for c, v in fixed.items() if isinstance(v, int)}
                    auto = [c for c in shown if c not in fixed]
                    # Auto columns get an equal share, never below their header minimum: the widest header rules.
                    need = sum(fixed.values()) + len(auto) * max(
                        [AUTO_COLUMN_PX] + [props.get(c, {}).get("custom.minWidth", 8 * len(c) + 40) for c in auto])
                    if need > w * PX_PER_COLUMN:
                        errs.append(f"{sec['title']} / {p['title']}: needs {need} px, {w * PX_PER_COLUMN} px at "
                                    "1080 (shorten the headers or drop columns)")
            for y, ps in rows.items():
                if sum(1 for p, _ in ps if p["type"] == "table") > 1:
                    errs.append(f"tab {sec['title']}: tables side by side at y={y}")
        return errs

    def write(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.render(), indent=1) + "\n")
