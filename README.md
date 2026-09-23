# content-lan — internal content showcase (content.lan)

Source of truth for the **content.lan** site and its approval API. Served from
the homelab k3s cluster (namespace `sites`, Deployment `content-site`) — NOT
from the zephyr workstation. Migrated 2026-09-23 under the standing directive
*"we do not want anything to be moved to zephyr"*.

## Layout

| Path | Served? | Purpose |
|------|---------|---------|
| `index.html` | yes | content.lan showcase. Discovers media by scraping nginx's `autoindex` of `/media/`, then fetches each asset's `.md`/`.json` sidecar for metadata (title, status, cast, cover). |
| `dashboard/index.html` | yes (`/dashboard/`) | team dashboard vhost (dashboard.lan) |
| `media/**` | yes (`/media/`) | the media tree: audio dramas, audio, images, video + per-asset sidecar `.md`/`.json` |
| `deploy/approval_api.py` | no (denied at the edge) | approval API — approve/reject a pending asset; rewrites `status:` in the asset's sidecar |
| `approval-log.jsonl` | no | seed copy of the decision log (runtime log lives on the cluster volume) |
| `index.html.bak-20260902` | yes | pre-migration snapshot, kept so no content revision is lost |

## How the cluster serves it

The site tree is a **cluster volume** (`PVC content-site-data`, seeded once from
this repo by the `content-site-seed` Job) so the approval API can rewrite
sidecars in place, exactly as it did on the workstation. The manifests live in
`reverb256/sites-k8s` (`helm/charts/content-site`, `helm/apps/content-site.yaml`)
and the `*.lan` vhosts in `reverb256/media-k8s`
(`cluster/addons/media-reverse-proxy/`).

- `content.lan` → `https://content.lan/` (edge: nginx-rp → `content-site.sites.svc.cluster.local:8080`)
- `dashboard.lan` → same service, prefixed to `/dashboard/`
- `content.lan:8791` → approval API (`/health`, `POST /`) via an nginx-rp listener

Because the served tree is a volume seeded from git, **content edits made in the
cluster (approval sidecar writes) are runtime state, not git history**: re-running
the seed Job resets the tree to this repo. Raise real content changes as commits
here, then re-run the seed Job.

## Approval API

```bash
curl -s http://content.lan:8791/health
curl -s -XPOST http://content.lan:8791/ -d '{"asset":"audio-dramas/x.mp3","action":"approve"}'
```

The kanban unblock that used to shell out to `hermes kanban ... unblock` cannot
run inside the cluster image (no hermes CLI). Those requests are appended to
`/site/pending/kanban-unblocks.jsonl` on the volume instead of being dropped;
the site-agency pipeline owns draining that queue.
