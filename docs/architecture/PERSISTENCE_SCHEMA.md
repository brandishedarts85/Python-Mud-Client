# Persistence Schema Contract

Profiles, persistence-safe automation, numpad macros, typed variables, and global client settings use the same document envelope.

```json
{
  "_mudclient": {
    "format": "python-mud-client",
    "kind": "profiles | automation | macros | variables | settings",
    "version": 1
  },
  "data": {}
}
```

## Compatibility rules

1. Historical unversioned files are **legacy version 0**.
2. Legacy files remain readable with their historical top-level shape.
3. Reading is side-effect free: loading a v0 file does not rewrite it.
4. New saves always write the current v1 envelope.
5. The first v0→v1 write creates a byte-for-byte `<filename>.pre-v1.bak` backup before replacing the live file.
6. That pre-v1 backup is one-shot and is never overwritten by later normal saves.
7. Invalid legacy data is not migrated by the explicit migration helper.
8. A schema version newer than this client supports raises `PersistenceVersionError`; fail-soft application loaders report the problem and keep the source untouched.
9. A versioned document whose `kind` does not match the loader is rejected.
10. Whole-file schema validation remains atomic before live application state is changed.

## Why reads do not auto-migrate

A read should not unexpectedly require write permission or mutate user data. Migration therefore happens only when the user actually commits a save, or when `migrate_legacy_persistence()` is explicitly invoked by a future migration UI/startup policy.

This keeps ordinary startup usable on read-only media and makes the backup/migration boundary explicit.

## Durability

The live document and migration backup use same-directory temporary files, file `fsync()`, atomic `os.replace()`, and best-effort parent-directory `fsync()` on platforms that support it.

If migration replacement fails after the backup succeeds, the legacy live file remains intact and the backup remains available.

## Variables payload

The `variables` document payload is a JSON object mapping validated variable names
to scalar values. Values may be strings, integers, finite floating-point numbers,
or booleans. Arrays, objects, null, NaN, and infinity are rejected. Variable names
are limited to 64 characters and use the same syntax accepted by `${name}`
substitution. The store is bounded to 1000 entries.
