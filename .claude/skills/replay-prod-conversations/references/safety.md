## Read production, write only to testing — and prove it

Restricted is **live production**. Real citizens are in there right now.

- **Never send a message to restricted.** The pull is read-only: it uses the admin API's
  GET endpoints and nothing else.
- The replayer refuses `--tenant restricted` outright, and before sending anything it
  opens one throwaway conversation and asks the admin API which tenant can see it. If
  that is not the tenant you named, it aborts having sent nothing.
- **That check exists because the flag does not route the traffic.** `--tenant` selects
  which config to snapshot and how to label the report. Where messages actually land is
  decided solely by `AGENT_API_KEY` in `.env`. Repoint that key and, without the probe,
  a run would write synthetic conversations into production while the report claimed
  "testing".
- New conversations WILL keep appearing in restricted while you work. That is production
  traffic, not your run. To confirm one is yours, look it up by id — a conversation is
  visible only to its owning tenant, so a replay id 404s on restricted.

If the user reports seeing unexpected conversations on restricted, do not reassure them —
check, by resolving the specific ids against both tenants:

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from tools.studio_admin import StudioAdmin, TENANTS
cid='<conversation-id>'
with StudioAdmin() as api:
    for n,t in TENANTS.items():
        try: api.get(f'/conversations/{cid}', tenant_id=t); print('lives in',n)
        except Exception: pass
"
```
