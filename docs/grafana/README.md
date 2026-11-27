# Grafana & Prometheus Integration

`hl-backend` exposes a zero-dependency Prometheus text exposition endpoint at:
- Public (via Dokploy Nginx): `https://<your-domain>/api/v1/metrics`
- Internal Docker network: `http://backend:8000/metrics`

## 1. Prometheus / VictoriaMetrics Scrape Config

Add this job to `prometheus.yml` (or VictoriaMetrics `vmagent` scrape config):

```yaml
scrape_configs:
  - job_name: "hyperliquid-quant-defi"
    scrape_interval: 60s
    scrape_timeout: 15s
    metrics_path: /api/v1/metrics
    scheme: https
    static_configs:
      - targets:
          - hl-quant.95-217-163-111.sslip.io
```

## 2. Import Dashboard into Grafana

1. Open **Dashboards → New → Import** in Grafana (`https://grafana.spy2money.com/dashboard/import`).
2. Upload `docs/grafana/hyperliquid_quant_defi_dashboard.json` (or paste its JSON).
3. Select your Prometheus / VictoriaMetrics datasource in the top dropdown and click **Import**.
