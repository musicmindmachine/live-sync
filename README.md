# live-sync

Prototype for synchronizing a subset of the Ableton Live Object Model between two Live instances over the internet.

## What is implemented

- Convex backend functions for unauthenticated room-scoped operation storage.
- A Live Remote Script package under `ableton/LiveSyncRemoteScript/` that:
  - snapshots a settable subset of song state,
  - computes Lamport-stamped delta operations,
  - pushes local edits to a localhost sidecar,
  - lets a local sidecar server handle push/pull requests while a separate local watch worker owns the official Convex Python WebSocket client outside of Live's embedded Python runtime,
  - receives remote invalidations from the sidecar and applies remote operations without polling Convex from inside Live,
  - merges conflicting writes with per-path last-write-wins CRDT semantics,
  - mirrors session-view and arrangement-view audio clip source files through a room-scoped media manifest plus R2-backed upload/download glue,
  - transfers `.asd` analysis sidecars when they exist next to the source audio file,
  - falls back to room snapshots when an op tail becomes too large or has already been compacted.
- An in-memory backend plus a mock two-client runner for local verification without Ableton or a deployed Convex app.

## Current sync scope

The Live adapter currently snapshots and applies:

- song tempo and groove amount,
- groove-pool metadata for existing grooves,
- track names, mute, solo, arm, and back-to-arranger state,
- mixer volume, panning, and send values,
- track, return-track, master-track, rack-chain, and drum-pad device trees, including nested chain mixer state, per-device parameter values, and native Live device insertion/deletion on Live 12.3+ builds that expose `insert_device(...)` / `delete_device(...)`,
- session-view clip state on existing clip slots, including clip-slot stop buttons, groove assignment by groove-pool index, MIDI notes, loop and marker settings, clip-grid view state, launch settings, color index, partial automation state (`has_envelopes` and remote clears), and common audio-clip settings such as RAM mode,
- session-view audio clip source references via project-relative media paths,
- arrangement-view clip state for existing tracks, including clip position, groove assignment by groove-pool index, MIDI notes, loop and marker settings, clip-grid view state, color index, partial automation state (`has_envelopes` and remote clears), and common audio-clip settings,
- return-track and master-track mixer values.

Structural edits such as creating or deleting tracks, scenes, or clip slots are not implemented yet. Session audio and MIDI clips can now be created in existing clip slots when the referenced media file is present locally, arrangement clips can be recreated from snapshot state on existing tracks, nested rack chains and drum-pad chains now reconcile recursively, and native Live devices can be inserted or deleted on Live 12.3+ builds that expose the public chain-mutation API. Return-chain structure inside racks is still parameter/state-only because the public LOM does not expose symmetric return-chain creation and deletion. On Live 12.2.x and older builds, device sync degrades to parameter/state mirroring for already-matching devices and skips structure changes so older clients do not roll device-chain edits back. Existing VST and Max devices can have parameter state and `selected_preset_index` mirrored. New VST and Max devices now use an experimental browser-based load path on top-level track chains when Live's internal Remote Script browser runtime is available, but this path is not part of the public LOM and is not yet verified across all device categories or nested chains. Full automation-envelope curve sync is still blocked by the public Live API: the LOM exposes clip note payloads, groove assignment, and `clear_all_envelopes`, but not a serializable/readable automation-envelope point payload that can be converged across clients.

## Compaction

The Convex backend now maintains a materialized room snapshot plus per-path LWW version metadata alongside the op log. That changes sync behavior in two useful ways:

- normal replicas still sync incrementally from recent ops,
- stale replicas reset from the latest snapshot instead of replaying an unbounded log,
- old ops are compacted in background batches so long sessions do not keep growing query cost forever.

This keeps `pullOps` bounded even after large sessions, and it avoids relying on a single query or mutation to process an arbitrarily long history. Conflicts within the currently synced path subset now converge as a real LWW CRDT using `(lamport, client_id, op_id)` ordering, so out-of-order delivery and concurrent writes do not depend on server arrival order. Structural Live edits are still out of scope, so this is not yet a sequence CRDT for track, scene, or clip creation/reordering.

## Media sync

The backend now has a separate room-scoped media plane:

- `mediaReferences` track which LOM path refers to which project-relative media path and content hash,
- `mediaAssets` dedupe uploaded files by content hash inside a room,
- HTTP routes under `.convex.site` drive register, upload, finalize, pull, and download flows,
- Cloudflare R2 stores the actual media objects and serves them by signed URL.

On the Live side, the script scans session clip slots for audio clips with a `file_path`, derives a project-relative target path, hashes the source file, registers it with Convex, uploads it to R2 if needed, and downloads missing remote files into the local project root. When a missing audio file arrives, the script re-applies the current shadow state so a pending remote audio clip can be created from the newly downloaded file.

If an `.asd` analysis file exists next to the source audio file, it is registered and transferred as a separate room-scoped media asset into the matching project-relative path so the remote project can reuse Live's analysis sidecar instead of regenerating it.

## Repo layout

- `convex/`: backend schema, functions, and HTTP routes.
- `ableton/LiveSyncRemoteScript/`: the Ableton-loadable Python package.
- `tools/mock_pair.py`: runs two simulated clients against the in-memory backend.
- `tests/`: Python tests for the diff/reconcile path.

## Convex setup

1. Install dependencies:

```bash
npm install
```

2. Configure or attach a Convex dev deployment and run:

```bash
npm run dev
```

3. After the deployment is configured, regenerate the real Convex `_generated` files if needed:

```bash
npm run codegen
```

Once the deployment is live, the sidecar should point at the Convex deployment URL, e.g. `https://<deployment>.convex.cloud`.

4. Configure Cloudflare R2 for the media layer:

```bash
npx convex env set R2_BUCKET <bucket-name>
npx convex env set R2_ENDPOINT <r2-endpoint>
npx convex env set R2_ACCESS_KEY_ID <access-key-id>
npx convex env set R2_SECRET_ACCESS_KEY <secret-access-key>
```

The current deployment does not have any `R2_*` environment variables set yet, so media upload/download will stay inactive until these are configured.

## Ableton Remote Script setup

Copy `ableton/LiveSyncRemoteScript/config.example.json` to `ableton/LiveSyncRemoteScript/config.json` and fill in your deployment values:

```json
{
  "deployment_url": "https://<your-convex-deployment>.convex.cloud",
  "site_url": "https://<your-convex-deployment>.convex.site",
  "room_id": "demo-room",
  "client_id": "studio-a",
  "project_root": "/absolute/path/to/your/Ableton Project"
}
```

Install the script into the recommended User Library location:

```bash
npm run install:live
```

That copies `ableton/LiveSyncRemoteScript` to `~/Music/Ableton/User Library/Remote Scripts/LiveSyncRemoteScript`, creates a local sidecar venv at `LiveSyncRemoteScript/.sidecar-venv`, and installs the official `convex` Python package into that venv. Live itself stays stdlib-only; a local sidecar server plus a separate watch worker are what load Convex's native runtime. To replace an existing install, run:

```bash
python3 tools/install_remote_script.py --force
```

If you specifically want to install into the Live app bundle instead, run:

```bash
npm run install:live:bundle -- --force
```

Environment variables with the same `LIVE_SYNC_*` names can override the file if you prefer.

## Testing in Live

1. Make sure your Convex deployment is running and `config.json` points at the correct `deployment_url`, `site_url`, `room_id`, and either a valid `project_root` or a saved Live Set so the script can infer the project root from `song.file_path`.
2. Install the remote script with `npm run install:live`.
3. Restart Ableton Live completely after each install or update.
4. Open `Settings` / `Preferences` -> `Link, Tempo & MIDI`.
5. In a `Control Surface` slot, choose `LiveSyncRemoteScript`.
6. Leave its input and output ports unset unless you later add MIDI I/O requirements.
7. On a second machine or second Live instance with the same room ID, install the same script. The Remote Script now adds a random runtime suffix to the configured `client_id` on each launch, so separate Live instances do not collide even if they share the same config file.
8. Test the currently synced fields: tempo, groove amount, groove-pool metadata, track name, mute, solo, arm, back-to-arranger, volume, panning, send values, track activator, crossfade assignment, panning mode, cue/crossfader on master, nested device parameter values on tracks/returns/master/rack chains/drum pads, native device-chain insertion on Live 12.3+, experimental top-level VST/Max browser loads, session and arrangement clip notes, groove assignment, clip-grid state, color index, stop-button state, partial automation clear state, and clip settings, session audio clip source files, and arrangement clip state on existing tracks.
9. Change one of those values on instance A and the remote invalidation should arrive over the Convex WebSocket immediately through the localhost sidecar. Local Ableton-side changes are emitted by LOM listeners, the watch worker keeps the room version state hot, and remote work is scheduled back onto Live's UI thread only when needed.
10. For a conflict test, change the same synced field on both instances at nearly the same time and confirm both sides converge to the same value.
11. For a media test, import or record an audio clip into an existing clip slot on instance A, wait for the file transfer to complete, and confirm the remote project downloads the file and can create the clip from the mirrored project-relative path. If a matching `.asd` file exists, confirm it appears next to the downloaded audio file.
12. For an arrangement test, create or move an arrangement clip on instance A, edit its notes or loop settings, and confirm the remote track arrangement converges after the next snapshot reconcile.
13. For a long-session recovery test, make many repeated edits on instance A, restart instance B, and confirm it converges from the latest room snapshot instead of needing the entire op history.

If Live does not show the script in the Control Surface list, the install path is wrong or Live was not restarted. If the script shows up but nothing syncs, verify the `.convex.cloud` deployment URL first, then tail `~/Library/Preferences/Ableton/LiveSyncRemoteScript.log` and the sidecar log path announced there.

## Local verification

Run the Python tests:

```bash
npm run test:python
```

Run the mock two-client reconciliation demo:

```bash
python3 tools/mock_pair.py
```

## Next steps

- Expand the adapter to cover devices, parameters, scenes, clips, and arrangement state.
- Add sequence-aware CRDT semantics for structural edits if you want simultaneous track, scene, or clip creation/reordering instead of path-level LWW only.
- Expand media sync beyond audio source files and `.asd` sidecars into broader project assets and derived data.
- Add a non-LOM plugin bridge if you need remote VST or Max device instantiation, since the public Live API only exposes native `insert_device(...)` on device chains.
- Add a non-LOM bridge if you need true automation-envelope curve sync, since the public Live API does not expose full envelope payloads for CRDT serialization.
- Add auth and room membership after the unauthenticated sync loop is stable.
