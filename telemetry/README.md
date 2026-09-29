# Voice telemetry contract

`contracts/<schema version>.json` lists what a voice turn sends to Langfuse: the
root name, the trace fields (`input`, `output`, `tags`, `level`, ...), the metadata
keys, the keys inside metadata blocks (`nested_keys`), the score names and the
outcomes. The version is `VOICE_TELEMETRY_SCHEMA_VERSION`
in `app/services/telemetry_stamps.py`, and `tests/test_voice_telemetry_contract.py`
checks the code against it.

A released contract never changes, because traces already in Langfuse follow it.
Any change to what a turn sends is a new schema version: a key, a trace field, a
score or an outcome added, renamed or removed (inside a block like `agent` too),
the root renamed, or a key that now means something else. For each one:

1. Bump `VOICE_TELEMETRY_SCHEMA_VERSION`, e.g. to `voice.turn.v2`.
2. Copy the contract to `contracts/voice.turn.v2.json` and make the change there.
   Leave the old file as it is.
3. Add the version to `telemetry/mappings/voice.yaml` in amul-oan-api. It extends
   the old one and lists only what moved; for an added key, two lines are enough.
4. A new outcome also needs a bucket in `voice_outcome_vocabulary` in
   `telemetry/eras.yaml` there.
5. Once it ships, add the new version's fingerprint to `RELEASED_CONTRACTS` in
   `tests/test_voice_telemetry_contract.py`:

   ```bash
   python -c "import json,hashlib; c=json.load(open('telemetry/contracts/voice.turn.v2.json')); c.pop('note',None); print(hashlib.sha256(json.dumps(c,sort_keys=True,separators=(',',':')).encode()).hexdigest())"
   ```

Names say what the value is: lowercase snake_case, never `data`, `id`, `result`,
`status`, `time`, `type` or `value` on their own.

The amul-oan-api side has to be merged first: `tests/test_voice_telemetry_readers.py`
checks its main branch for the outcome buckets and the mapping.

Every turn is also stamped with `service` and `release`. `release` is the git
commit of the running code: the checkout's HEAD when the repo is mounted, or
`GIT_SHA` passed to `docker build` (`--build-arg GIT_SHA=$(git rev-parse HEAD)`).
With neither, it reads `unknown`.

Step by step, with every file to edit: `docs/TELEMETRY_CHANGES.md` in amul-oan-api.
Full rules: `docs/TELEMETRY_STANDARD.md` there too.
