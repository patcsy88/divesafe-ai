# First real run of the production stack (ADR 0024, condition 1)

Purpose: show, once, on the real host with the real secrets, that an assessment created through
the API reaches the model host, is stored as `PENDING_HUMAN`, can be decided once (write-once), and
survives a restart. **This is a procedure to run by hand. Nobody has run it yet**, so ADR 0024 stays
PROPOSED until you record the result there.

This is decision support. A successful run shows the plumbing works; it says nothing about dive
safety. With no signed ruleset the result cannot be `GO`, and that is expected.

## What leaves the machine

Every assessment sends its plan fields, evidence, rule results and findings to the model host named in
`DIVESAFE_LLM_BASE_URL` (the opt-in is stated in the compose file). The API also calls the data
sources from inside the container: Open-Meteo and data.gov.my, and MET Malaysia if you add a token.
PHN is off by default. Use a plan for a real site and a future date, nothing private. Do not paste
any secret into a chat, a ticket or a file inside the repository.

## Before you start

- Docker with Compose, and the images built (`--build` below needs the network once).
- Run everything from the repository root, in one terminal, so the shell variables below stay set.
- `docker/secrets/` and `docker/production.env` are git-ignored. Check with `git status` at the end.

## 1. Settings and secrets

```sh
cp docker/production.env.example docker/production.env
# edit docker/production.env: set DIVESAFE_LLM_MODEL and DIVESAFE_EVIDENCE_MAX_AGE_MINUTES
```

`DIVESAFE_EVIDENCE_MAX_AGE_MINUTES` has no built-in default and no recorded reason yet (ADR 0024,
condition 4). For this run pick a value and write down that it is a test value; do not copy it into
a real deployment without a reason.

```sh
sh docker/make-secrets.sh docker/secrets        # database secrets; never overwrites; prints no secret

# the model host key: typed, not echoed, not in shell history
read -rs "K?model host key: "; echo      # bash: read -rs -p "model host key: " K; echo
printf '%s' "$K" > docker/secrets/divesafe_llm_api_key; unset K
chmod 0444 docker/secrets/divesafe_llm_api_key

# a test API key for yourself, kept only in this shell; only its hash is written to disk
KEY=$(openssl rand -hex 32)
HASH=$(printf '%s' "$KEY" | shasum -a 256 | cut -d' ' -f1)
printf '{"%s": {"actor": "first-run", "roles": ["assessor", "decider", "viewer"]}}' "$HASH" \
  > docker/secrets/divesafe_api_key_hashes
chmod 0444 docker/secrets/divesafe_api_key_hashes
unset HASH
```

The one key holds all three roles so a single person can do the whole run. A real deployment should
give the roles to different people. The MET token file is created empty; leave it empty unless you
have a token, in which case that source is simply missing and the result says so.

## 2. Start the stack

```sh
docker compose -f docker/docker-compose.production.yml --env-file docker/production.env up -d --build --wait api
curl -sS http://127.0.0.1:8000/health
```

Expect `"status":"ok"` and `"storage":"durable"`. If `api` does not become healthy, run
`docker compose -f docker/docker-compose.production.yml logs api db-init` and stop there. The API
refuses to start in production when a check fails (agent stage off, owner is a superuser, guard
triggers not `ENABLE ALWAYS`, and so on); that is the design, not a bug to work around.

Check that no key is accepted without credentials:

```sh
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/v1/sites        # expect 401
```

## 3. Create one assessment

```sh
START=$(date -u -v+1d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '+1 day' +%Y-%m-%dT%H:%M:%SZ)
curl -sS -i -X POST http://127.0.0.1:8000/v1/assessments \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: first-run-$(date +%s)-0001" \
  -d "{\"site_id\":\"my-pahang-pulau-tioman\",\"planned_start\":\"$START\",\"planned_duration_minutes\":45,\"max_depth_m\":18}"
```

Expect `202` with a `Location: /v1/assessments/<id>` header and a run status. Copy the id:

```sh
ID=<paste the id>
```

Poll until the body is an assessment, not a run status. Allow up to a few minutes (a model call
can take about two minutes):

```sh
curl -sS -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/v1/assessments/$ID | python3 -m json.tool
```

What to look for, and what to write down:
- The status is `PENDING_HUMAN`, and the recommendation is not `GO`.
- Whether the agent stage ran, or the record lists an agent issue (for example the model host was
  unreachable). Either is a valid finding; the deterministic result stands.
- The evidence issues for each source that failed or was missing.
- That the model host actually received a call. Look at your model host's own log, or
  `docker compose -f docker/docker-compose.production.yml logs api` (it does not log prompts).

## 4. Decide it, once

Accepting or overriding is yours. Use the recommendation as the decision to accept it, or a different
one with a rationale to override (an override without a rationale is refused with 422).

```sh
curl -sS -X POST http://127.0.0.1:8000/v1/assessments/$ID/decision \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"decision":"NO-GO","rationale":"first-run test of the human gate"}' | python3 -m json.tool
```

Then try again with a different decision and expect `409` ("already has a decision"):

```sh
curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/v1/assessments/$ID/decision \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"decision":"CAUTION","rationale":"must be refused"}'
```

## 5. Restart with the data in the volume

```sh
docker compose -f docker/docker-compose.production.yml --env-file docker/production.env restart api
docker compose -f docker/docker-compose.production.yml --env-file docker/production.env up -d --wait api
curl -sS -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/v1/assessments/$ID | python3 -m json.tool
```

Expect the same assessment with the same decision. Then a full stop and start, keeping the volume:

```sh
docker compose -f docker/docker-compose.production.yml --env-file docker/production.env down
docker compose -f docker/docker-compose.production.yml --env-file docker/production.env up -d --wait api
curl -sS -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/v1/assessments/$ID | python3 -m json.tool
```

Never use `down -v` on a stack whose data you want; it deletes the volume.

## 6. Clean up and record

```sh
unset KEY ID START
git status --short        # expect nothing: secrets and production.env are ignored
```

Keep the volume if this is your real deployment. If it was only a trial, `down -v` and
`rm -rf docker/secrets` remove the data and the secrets.

Record in ADR 0024, condition 1, with the date: the host OS, whether the model host was reached,
the final status, whether the agent stage ran or listed an issue, the 409 on the second decision,
and the result after each restart. Say what you did not see. Do not paste the key, the hash, a
prompt, or a model reply into the ADR.

## What this run does not show

- That a restore works with this data: run `docs/backup-and-restore.md` afterwards.
- A signed ruleset: none exists, so there is no `GO` path.
- The web UI, a reverse proxy or TLS in front of the API.
- Anything about which source the data came from beyond the evidence issues you read.
