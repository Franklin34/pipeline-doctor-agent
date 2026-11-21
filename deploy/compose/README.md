# deploy/compose

Local stack: Redis, Postgres with pgvector, and the ingress. Optional observability (Prometheus, Grafana, Jaeger) behind a profile.

```bash
cp .env.example .env            # then set POSTGRES_PASSWORD and WEBHOOK_SHARED_SECRET
make up                          # redis + postgres + ingress
docker compose -f deploy/compose/docker-compose.yml --profile observability up -d   # adds Prometheus :9090, Grafana :3000, Jaeger :16686
make down
```

The worker is run on the host (`uv run python -m pipeline_doctor_agent.worker`), not in compose. The cloud deployment is in `deploy/terraform`.
