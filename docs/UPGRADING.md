# Upgrading

## From v1.5.x to v1.6.0+ (plugin rename: deltachat-platform → deltachat)

v1.6.0 renamed the plugin's registered platform id from `deltachat-platform`
to `deltachat` (see the [CHANGELOG](../CHANGELOG.md)). A plain `git pull` in
your existing install picks up the code, but three things need attention
afterward.

### 1. The plugin install directory (optional)

Not required — the plugin's identity comes from `plugin.yaml`'s `name:`
field, not the directory name, so `~/.hermes/plugins/deltachat-platform/`
keeps working fine after a `git pull` inside it. Rename it to
`~/.hermes/plugins/deltachat/` only if you want the directory name to match
current docs/examples:

```bash
mv ~/.hermes/plugins/deltachat-platform ~/.hermes/plugins/deltachat
```

### 2. `config.yaml` (required — the plugin will show as "not enabled" otherwise)

Each Hermes profile that runs this plugin has its own `config.yaml`
(`~/.hermes/profiles/<profile>/config.yaml`, or `~/.hermes/config.yaml` for
the default profile). Two keys there are keyed by the *old* plugin name and
must be renamed by hand — Hermes matches plugin enablement by exact key, so
leaving these as `deltachat-platform` silently disables the plugin (no
error, it just stops loading):

```yaml
plugins:
  enabled:
    - deltachat-platform   # → deltachat

platforms:
  deltachat-platform:      # → deltachat
    enabled: true
    extra:
      ... (leave all nested settings exactly as they are)
```

Edit both keys, keep every nested `extra:` value under `platforms:` as-is,
then verify:

```bash
hermes -p <profile> plugins list   # deltachat should show "enabled"
```

(`hermes plugins list` with no `-p` checks the default profile.)

### 3. Existing chat routing/session history (required to avoid losing it)

Hermes persists which chat maps to which conversation session keyed by
`agent:<name>:<platform>:...`. Those keys embed the *old* platform id too —
left alone, every existing DM/group chat becomes unparseable at startup
("`'deltachat-platform' is not a valid Platform`") and warnings get skipped,
which orphans that chat's session (it starts fresh instead of resuming).

Run the migration script per affected profile, gateway stopped first:

```bash
hermes gateway stop -p <profile>   # or: hermes gateway stop (default profile)

# Dry run first — reports what would change, writes nothing:
python3 scripts/migrate_deltachat_platform_rename.py ~/.hermes/profiles/<profile>

# Looks right? Apply it (backs up state.db and sessions.json first):
python3 scripts/migrate_deltachat_platform_rename.py --apply ~/.hermes/profiles/<profile>

hermes gateway start -p <profile>
```

For the default profile, point it at `~/.hermes` instead of a profile
subdirectory. Repeat for every profile that has ever run this plugin.

The script only rewrites the platform id (`session_key`, `platform`,
`origin.platform`); it never touches `session_id` or message content, and
it's safe to re-run (a second run finds nothing left to migrate).

### Still seeing "not a valid Platform" after all three steps?

That combination (stale `config.yaml` keys + un-migrated routing state) is
exactly what produces it — double check step 2 actually took effect with
`hermes plugins list` before re-running the migration script.
