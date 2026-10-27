# agent-readiness

Ranks the objects in an AtScale SML semantic model by how well they will work with AI agents, using Jev to judge confusability, name accuracy and description quality. See the docstring in `readiness.py` for details; weights and thresholds are at the top of that file.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TYPESAFE_API_KEY=...
python readiness.py PATH_TO_SML_REPO --dump-state   # review exactly what would be sent
python readiness.py PATH_TO_SML_REPO                # writes results.json and report.html
python readiness.py --from-results results.json     # re-render without calling Jev
```

`PATH_TO_SML_REPO` must contain `metrics/`, `calculations/`, `dimensions/` and `models/`. Only those folders are read. Only name, label, description, folder and referenced measure names are sent; expressions never are.
