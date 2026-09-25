# Voice telemetry contract

`contracts/<schema version>.json` lists the metadata keys and outcomes a voice
turn sends to Langfuse. The version is `VOICE_TELEMETRY_SCHEMA_VERSION` in
`app/services/telemetry_stamps.py`, and `tests/test_voice_telemetry_contract.py`
checks the code against it.

- Added a key: list it in the contract. No version change.
- Added an outcome: list it in the contract, and add it to `voice_outcome_vocabulary`
  in `telemetry/eras.yaml` in amul-oan-api.
- Renamed or removed a key, or changed what one means: bump the version, add a
  contract file for it, and add the version to `telemetry/mappings/voice.yaml`
  in amul-oan-api. It can extend the old version and list only what moved.

Step by step, with every file to edit: `docs/TELEMETRY_CHANGES.md` in amul-oan-api.
Full rules: `docs/TELEMETRY_STANDARD.md` there too.
