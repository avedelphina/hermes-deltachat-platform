# Upgrading

## Coming from v1.5.x or earlier, straight to v1.7.0+

Nothing to do. The plugin's name (`deltachat-platform`), install directory
convention, and account-data directory are all unchanged from v1.5.x — see
below for why.

## If you have a v1.6.0–v1.6.4 install

v1.6.0 renamed the plugin from `deltachat-platform` to `deltachat`; v1.7.0
reverted that rename (`-platform` turned out to be Hermes's own naming
convention for messaging platform plugins, not something to drop). If you
updated to any version in the v1.6.0–v1.6.4 range, a plain `git pull` to
v1.7.0+ picks up the code, but three things need attention afterward.

### 1. The plugin install directory (optional)

Not required — the plugin's identity comes from `plugin.yaml`'s `name:`
field, not the directory name. Rename it back only if you want the
directory name to match current docs/examples:

```bash
mv ~/.hermes/plugins/deltachat ~/.hermes/plugins/deltachat-platform
```

### 2. `config.yaml` (required — the plugin will show as "not enabled" otherwise)

Each Hermes profile that runs this plugin has its own `config.yaml`
(`~/.hermes/profiles/<profile>/config.yaml`, or `~/.hermes/config.yaml` for
the default profile). Two keys there are keyed by the *v1.6.x* plugin name
and must be renamed back by hand — Hermes matches plugin enablement by
exact key, so leaving these as `deltachat` silently disables the plugin (no
error, it just stops loading):

```yaml
plugins:
  enabled:
    - deltachat   # → deltachat-platform

platforms:
  deltachat:      # → deltachat-platform
    enabled: true
    extra:
      ... (leave all nested settings exactly as they are)
```

Edit both keys, keep every nested `extra:` value under `platforms:` as-is,
then verify:

```bash
hermes -p <profile> plugins list   # deltachat-platform should show "enabled"
```

(`hermes plugins list` with no `-p` checks the default profile.)

### 3. Existing chat routing/session history (required to avoid losing it)

Hermes persists which chat maps to which conversation session keyed by
`agent:<name>:<platform>:...`. Those keys embed the *v1.6.x* platform id too
— left alone, every existing DM/group chat becomes unparseable at startup
("`'deltachat' is not a valid Platform`") and gets skipped, which orphans
that chat's session (it starts fresh instead of resuming).

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

The script's default direction restores `deltachat-platform` (what you want
here). It only rewrites the platform id (`session_key`, `platform`,
`origin.platform`); it never touches `session_id` or message content, and
it's safe to re-run (a second run finds nothing left to migrate).

### Still seeing "not a valid Platform" after all three steps?

That combination (stale `config.yaml` keys + un-migrated routing state) is
exactly what produces it — double check step 2 actually took effect with
`hermes plugins list` before re-running the migration script.

## Reference: the v1.5.x → v1.6.x direction

If you ever need to go the other way (`deltachat-platform` → `deltachat` —
e.g. testing a v1.6.x checkout against a fresh profile), the same three
steps apply with the names swapped, and the migration script takes
`--reverse`:

```bash
python3 scripts/migrate_deltachat_platform_rename.py --apply --reverse ~/.hermes/profiles/<profile>
```
