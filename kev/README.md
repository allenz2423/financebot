# Local Kev decision service

This optional service exposes Kev-4B's Jev-compatible typed-decision endpoint
to Delilah at `http://kev:8009/v1/systemone`.

Start it with:

```bash
docker compose --profile kev up -d kev
```

The model revision and serving command are intentionally isolated from the
Delilah image. The Compose file pins a reviewed upstream commit by default.
Set `KEV_REF` only when deliberately upgrading the sidecar, and run the
compatibility tests before promoting it. `KEV_RUN` selects the loaded
checkpoint; the default is `jaredpalmer/kev-4b`, while Delilah sends the API
alias `kev-latest`.

For a host-run Delilah process, point `KEV_LOCAL_URL` at
`http://127.0.0.1:8009/v1/systemone`. Under Compose, Delilah uses
`http://kev:8009/v1/systemone` through `KEV_DOCKER_LOCAL_URL` so a host-local
address cannot accidentally be used from inside the container.
