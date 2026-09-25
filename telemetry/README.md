# Voice telemetry contract

`contracts/<schema version>.json` lists what a voice turn sends to Langfuse: the
root name, the trace fields, the metadata keys, the keys inside metadata blocks
(`nested_keys`) and the outcomes. The version is `VOICE_TELEMETRY_SCHEMA_VERSION`
in `app/services/telemetry_stamps.py`, and `tests/test_voice_telemetry_contract.py`
checks the code against it.

- Added a metadata key: list it in the contract. No version change.
- Added a key inside a block (e.g. `agent`): nothing to do.
- Added an outcome: list it in the contract, and add it to `voice_outcome_vocabulary`
  in `telemetry/eras.yaml` in amul-oan-api.
- Renamed or removed any of the above, renamed the root, or changed what a key
  means: bump the version, add a contract file for it, and add the version to
  `telemetry/mappings/voice.yaml` in amul-oan-api. It can extend the old version
  and list only what moved.

Every turn is also stamped with `service` and `release`. `release` is the git
commit of the running code: the checkout's HEAD when the repo is mounted, or
`GIT_SHA` passed to `docker build` (`--build-arg GIT_SHA=$(git rev-parse HEAD)`).
With neither, it reads `unknown`.

Step by step, with every file to edit: `docs/TELEMETRY_CHANGES.md` in amul-oan-api.
Full rules: `docs/TELEMETRY_STANDARD.md` there too.
