#!/usr/bin/env python3
"""
readiness.py: which objects in an AtScale SML model need refactoring to work
better with AI agents?

Agents pick metrics and dimensions by name and description. Jev judges, per
object, whether it is easily confused with another object, whether its name
matches what it computes, and how good its description is. This script loads
the model, builds the payload, asks Jev, scores the answers, and writes a
ranked report.

Reads only metrics/, calculations/, dimensions/ and models/. Sends Jev only
name, label, description, folder and (for calculations) referenced measure
names -- never expressions.

  python readiness.py REPO                       # call Jev, write results.json + report.html
  python readiness.py REPO --dump-state          # print the payload and exit (no Jev call)
  python readiness.py --from-results results.json  # re-render report.html (no Jev call)
"""
import argparse
import html
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ============================================================== CONFIG =====
JEV_MODEL = "jev-1.13.0"

# Readiness = 100 * weighted average of these (each 0..1). name_match only
# applies to metrics; for everything else the other weights are renormalized.
WEIGHTS = {
    "distinct": 0.40,      # 1 - P(a business user could mean another listed object)
    "description": 0.30,   # description Score / 3
    "name_match": 0.30,    # P(name accurately describes what it computes)
}

GREEN_MIN = 75   # score >= this is green
AMBER_MIN = 50   # score >= this is amber, below is red

# Thresholds for writing a reason; they only pick which Jev answers get a sentence.
CONFUSED_MIN = 0.50      # P(could mean another object) at or above this -> "easily confused"
NAME_MATCH_MIN = 0.50    # P(name matches) below this -> "name may not match"
DESCRIPTION_MIN = 1.5    # description score below this -> "weak description"

MAX_CALL_TOKENS = 64_000  # Jev limit per call; estimated as len(json)/4
CHARS_PER_TOKEN = 4


# ============================================================== loading =====
class Obj:
    def __init__(self, kind, name, label, description, file, folder=None, refs=None):
        self.kind = kind  # metric | calc | attribute | calc_member
        self.name = (name or "").strip()
        self.label = (label if label is not None else name or "").strip()
        self.description = (description or "").strip()
        self.file = file
        self.folder = folder
        self.refs = refs or []


def load(root):
    """Load only metrics/, calculations/, dimensions/ and models/. A calculation's
    expression is read only to pull out [Measures].[X] names and is never kept."""
    folders = {}
    for mf in sorted((root / "models").glob("*.yml")):
        m = yaml.safe_load(mf.read_text()) or {}
        for e in m.get("metrics", []) or []:
            folders[e["unique_name"].strip()] = e.get("folder")

    objs = []
    for f in sorted((root / "metrics").glob("*.yml")) + sorted((root / "calculations").glob("*.yml")):
        d = yaml.safe_load(f.read_text()) or {}
        name = d.get("unique_name", f.stem)
        kind = {"metric": "metric", "metric_calc": "calc"}.get(
            d.get("object_type"), "calc" if f.parent.name == "calculations" else "metric")
        refs = sorted({r.strip() for r in re.findall(r"\[Measures\]\.\[([^\]]+)\]", d.get("expression") or "")})
        objs.append(Obj(kind, name, d.get("label"), d.get("description"), f,
                        folder=folders.get(name.strip()) or d.get("folder"), refs=refs))

    for f in sorted((root / "dimensions").glob("*.yml")):
        d = yaml.safe_load(f.read_text()) or {}
        dim = d.get("unique_name", f.stem)

        def attr(a, kind="attribute", folder=dim):
            objs.append(Obj(kind, a["unique_name"], a.get("label"), a.get("description"), f, folder=folder))

        for h in d.get("hierarchies", []) or []:
            for lvl in h.get("levels", []) or []:
                for a in (lvl.get("secondary_attributes") or []) + (lvl.get("aliases") or []):
                    attr(a)
                for m in lvl.get("metrics", []) or []:  # metrics defined inside a level
                    attr(m, "metric", m.get("folder"))
        for la in d.get("level_attributes", []) or []:
            attr(la)
        for cg in d.get("calculation_groups", []) or []:
            for cm in cg.get("calculated_members", []) or []:
                attr(cm, "calc_member", cg.get("folder") or cg["unique_name"])
    return objs


def group_objects(objs):
    groups = {"metrics": [], "dimension attributes": [], "time calculations": []}
    for o in objs:
        key = {"metric": "metrics", "calc": "metrics", "attribute": "dimension attributes",
               "calc_member": "time calculations"}[o.kind]
        groups[key].append(o)
    return {k: v for k, v in groups.items() if v}


KIND_LABEL = {"metric": "metric", "calc": "calculation", "attribute": "attribute",
              "calc_member": "time calculation"}


# ============================================================== payload =====
def build_state(members):
    """Exactly what leaves the machine. Nothing else is ever sent."""
    rows = []
    for o in members:
        row = {"name": o.name, "label": o.label, "description": o.description}
        if o.folder:
            row["folder"] = o.folder
        if o.refs:
            row["measures_referenced"] = o.refs
        rows.append(row)
    return {"objects": rows}


def question_specs(members):
    """Plain dicts, turned into SDK objects only at call time. Keys are
    '<id>:<facet>' where id is the object's position in this batch."""
    specs = {}
    for i, o in enumerate(members):
        lbl = o.label
        others = {f"o{j}": m.label for j, m in enumerate(members) if j != i}
        others["none"] = "None of the other listed objects"
        specs[f"o{i}:confusable"] = {"type": "noul", "instructions": (
            f"A business user asking for '{lbl}' could plausibly mean another listed object instead.")}
        specs[f"o{i}:confused_with"] = {"type": "choice", "criteria": others, "instructions": (
            f"Which other listed object is '{lbl}' most easily confused with?")}
        specs[f"o{i}:description"] = {"type": "score", "instructions": (
            f"The description of '{lbl}'."), "criteria": [
            "Missing, or just repeats the name",
            "Vague; does not say what is measured",
            "Says what is measured",
            "Says what it measures and how it differs from similar objects"]}
        if o.kind in ("metric", "calc"):
            specs[f"o{i}:name_match"] = {"type": "noul", "instructions": (
                f"The name '{lbl}' accurately describes what this object computes, "
                f"judging by its description and measures_referenced.")}
    return specs


def tokens(x):
    return len(json.dumps(x, ensure_ascii=False)) // CHARS_PER_TOKEN


def batches(members):
    """One batch per group; halved only if the estimate exceeds Jev's call limit.
    Objects in different batches are never compared with each other."""
    if len(members) > 1 and tokens(build_state(members)) + tokens(question_specs(members)) > MAX_CALL_TOKENS:
        mid = len(members) // 2
        return batches(members[:mid]) + batches(members[mid:])
    return [members]


# ================================================================== Jev =====
def ask_jev(client, members):
    from typesafe_sdk import Choice, Noul, Score
    build = {"noul": lambda s: Noul(instructions=s["instructions"]),
             "choice": lambda s: Choice(instructions=s["instructions"], criteria=s["criteria"]),
             "score": lambda s: Score(instructions=s["instructions"], criteria=s["criteria"])}
    specs = question_specs(members)
    resp = client.system_one(state=build_state(members),
                             questions={k: build[s["type"]](s) for k, s in specs.items()})
    answers = defaultdict(dict)
    for key, a in resp.answers.items():
        i, facet = key.split(":")
        if facet == "confusable" or facet == "name_match":
            answers[i][facet] = {"noul": a.noul}
        elif facet == "confused_with":
            answers[i][facet] = {"choice": a.choice, "probabilities": dict(a.probabilities)}
        else:
            answers[i][facet] = {"score": float(a.score)}
    return resp.model, answers


# ================================================================ scoring ===
def score_object(ans):
    parts = [(1 - ans["confusable"]["noul"], WEIGHTS["distinct"]),
             (ans["description"]["score"] / 3, WEIGHTS["description"])]
    if "name_match" in ans:
        parts.append((ans["name_match"]["noul"], WEIGHTS["name_match"]))
    return round(100 * sum(v * w for v, w in parts) / sum(w for _, w in parts))


def band(score):
    return "green" if score >= GREEN_MIN else "amber" if score >= AMBER_MIN else "red"


def reasons(ans, names):
    out = []
    cw = ans["confused_with"]
    if ans["confusable"]["noul"] >= CONFUSED_MIN and cw["choice"] != "none":
        other = names.get(cw["choice"], cw["choice"])
        out.append(f"Easily confused with {other} (p={cw['probabilities'].get(cw['choice'], 0):.2f})")
    if "name_match" in ans and ans["name_match"]["noul"] < NAME_MATCH_MIN:
        out.append("Name may not match what it computes")
    if ans["description"]["score"] < DESCRIPTION_MIN:
        out.append("Weak description")
    return out


def rows_from_results(res):
    """Recompute scores from the raw answers, so weight tweaks apply on re-render."""
    rows = []
    for b in res["batches"]:
        names = {f"o{i}": o["label"] for i, o in enumerate(b["objects"])}
        for i, o in enumerate(b["objects"]):
            ans = b["answers"][f"o{i}"]
            s = score_object(ans)
            rows.append({**o, "score": s, "band": band(s), "reasons": reasons(ans, names)})
    rows.sort(key=lambda r: (r["score"], r["label"]))
    return rows


# ================================================================= report ===
def render(res):
    rows = rows_from_results(res)
    model = round(sum(r["score"] for r in rows) / len(rows)) if rows else 0
    counts = {c: sum(r["band"] == c for r in rows) for c in ("red", "amber", "green")}
    e = html.escape
    body = []
    for r in rows:
        why = "; ".join(r["reasons"]) or "No issues found"
        extra = f" &middot; folder: {e(r['folder'])}" if r.get("folder") else ""
        if r.get("measures_referenced"):
            extra += f" &middot; measures: {e(', '.join(r['measures_referenced']))}"
        desc = e(r["description"]) if r["description"] else "<em>(no description)</em>"
        body.append(
            f"<tr class=row><td>{e(r['label'])}</td><td>{e(KIND_LABEL[r['kind']])}</td>"
            f"<td class=file>{e(r['file'])}</td><td><span class='pill {r['band']}'>{r['score']}</span></td>"
            f"<td>{e(why)}</td></tr>"
            f"<tr class=detail hidden><td colspan=5><b>Description sent to Jev:</b> {desc}"
            f"<div class=meta>name: {e(r['name'])}{extra}</div></td></tr>")
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Agent readiness report</title><style>
body{{font:15px/1.45 system-ui,sans-serif;margin:2rem auto;max-width:1100px;padding:0 1rem;color:#1a1a1a}}
h1{{margin:0 0 .25rem}} .sub{{color:#666;margin-bottom:1.5rem}}
.hero{{display:flex;align-items:center;gap:1.5rem;margin-bottom:1.5rem}}
.big{{font-size:3rem;font-weight:700;padding:.2rem 1rem;border-radius:.5rem}}
table{{border-collapse:collapse;width:100%}} th,td{{text-align:left;padding:.5rem .6rem;border-bottom:1px solid #e5e5e5;vertical-align:top}}
th{{background:#f6f6f6}} tr.row{{cursor:pointer}} tr.row:hover{{background:#fafafa}}
.file{{color:#666;font-size:.85rem}} .meta{{color:#666;font-size:.85rem;margin-top:.4rem}}
tr.detail td{{background:#fafafa}}
.pill{{display:inline-block;min-width:2.4rem;text-align:center;padding:.1rem .5rem;border-radius:1rem;font-weight:600}}
.red{{background:#fbd5d5;color:#8a1111}} .amber{{background:#fde8b8;color:#7a4b00}} .green{{background:#d3f0d8;color:#14602a}}
</style></head><body>
<h1>Agent readiness report</h1>
<div class=sub>Jev {e(res['jev_model'])} &middot; {e(res['timestamp'])} &middot; {e(res['repo'])}</div>
<div class=hero><div class='big {band(model)}'>{model}</div>
<div>Model readiness (mean of {len(rows)} objects, 0&ndash;100)<br>
{counts['red']} red &middot; {counts['amber']} amber &middot; {counts['green']} green</div></div>
<table><thead><tr><th>Object</th><th>Type</th><th>File</th><th>Score</th><th>Reasons</th></tr></thead>
<tbody>{''.join(body)}</tbody></table>
<script>document.querySelectorAll('tr.row').forEach(r=>r.onclick=()=>{{const d=r.nextElementSibling;d.hidden=!d.hidden}})</script>
</body></html>"""


# ==================================================================== CLI ===
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", nargs="?", default=".", help="folder containing metrics/, calculations/, dimensions/, models/")
    ap.add_argument("--dump-state", action="store_true", help="print the payload and exit without calling Jev")
    ap.add_argument("--from-results", metavar="FILE", help="re-render report.html from a results.json")
    args = ap.parse_args()

    if args.from_results:
        res = json.loads(Path(args.from_results).read_text())
        Path("report.html").write_text(render(res))
        print("Wrote report.html")
        return

    root = Path(args.repo).expanduser().resolve()
    missing = [d for d in ("metrics", "calculations", "dimensions", "models") if not (root / d).is_dir()]
    if missing:
        sys.exit(f"{root} is missing {', '.join(m + '/' for m in missing)}; point at the folder that contains them")
    groups = group_objects(load(root))
    plan = [(g, b) for g, members in groups.items() for b in batches(members)]

    if args.dump_state:
        print(json.dumps([{"group": g, "state": build_state(b)} for g, b in plan], indent=2, ensure_ascii=False))
        return

    from typesafe_sdk import TypeSafeClient
    client = TypeSafeClient(model=JEV_MODEL)
    res = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "repo": root.name, "jev_model": JEV_MODEL, "batches": []}
    for g, members in plan:
        print(f"{g}: {len(members)} objects", file=sys.stderr)
        model, answers = ask_jev(client, members)
        res["jev_model"] = model
        res["batches"].append({
            "group": g,
            "payload": build_state(members),
            "answers": answers,
            "objects": [{"name": o.name, "label": o.label, "description": o.description,
                         "folder": o.folder, "measures_referenced": o.refs, "kind": o.kind,
                         "file": str(o.file.relative_to(root))} for o in members]})
    Path("results.json").write_text(json.dumps(res, indent=2, ensure_ascii=False))
    Path("report.html").write_text(render(res))
    print("Wrote results.json and report.html")


if __name__ == "__main__":
    main()
