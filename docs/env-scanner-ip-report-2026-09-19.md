# `.env` / secrets scanner — source IP report

**Date:** 2026-09-19
**Host public IP:** `98.14.10.98`
**Log source:** `nginx-proxy-manager-app-1` → `/data/logs/proxy-host-*_access.log`, `/data/logs/fallback_http_access.log`
**Window analysed:** 2026-09-13 11:21 → 2026-09-19 14:37 (+1000)

## Why the bot's own logs only showed `172.24.0.1`

The request path is:

```
scanner ──▶ host 98.14.10.98 :80/:443
             └─ nginx-proxy-manager (container 172.19.0.3)
                  └─ proxy_pass 192.168.0.167:8000   # the host's own LAN IP
                       └─ iptables DNAT 172.24.0.7:8000  # delilah_bot
                            └─ uvicorn/starlette access log sees 172.24.0.1
```

Docker's userland proxy rewrites the source of the forwarded connection to the
bridge gateway (`172.24.0.1`), so the app-level log can never show the real
client. The real source is visible in two places:

- the host conntrack table (`/proc/net/nf_conntrack`) — shows `src=172.19.0.3 dst=192.168.0.167 dport=8000`, i.e. the traffic originates from the **Nginx Proxy Manager container**, and
- NPM's own access logs, which carry the true `$remote_addr`.

**Note:** two proxy hosts forward to host port `8000` — the **delilah bot's FastAPI
port** — and therefore publish the bot API to the internet:

| Proxy host | Upstream |
|---|---|
| `plaid.delilah.incoming.nyc` | `192.168.0.167:8000` (delilah_bot) |
| `concierge.incoming.nyc` | `192.168.0.167:8000` (delilah_bot) |

This is why the `.env` sweep landed in the bot's uvicorn logs. (For reference, the
rest of the mapping is `omniroute.incoming.nyc → :20128`, `kuma → :3001`,
`zipline → :3000`, `qwen → :4170`, `bitwarden → vaultwarden:80`, `ntfy → :80`,
`refine.shows.pics → 192.168.0.233:8766`.)

## Outcome: nothing was exfiltrated

Verified against both log layers:

- **`delilah_bot` (port 8000):** 100% of requests returned `404`. Zero non-404
  responses in its entire log — the bot never served a secret path.
- **Every real secret-file request returned `404`.** The handful of `200`s that
  appear in the NPM logs on `.env`/`.git` paths are **SPA catch-all shells, not
  files**: Uptime-Kuma returns a byte-identical 625-byte response for *every*
  path (`/.env`, `/.git/config`, `/wp-configs.php` — all `len=625`), and Zipline
  returns a byte-identical 984-byte response. Identical length across different
  paths = the app's index/error page, not file content.
- The only genuine `200`s on "sensitive"-looking paths were legitimate Vaultwarden
  asset requests (`/icons/*.png`).

So the sweep found nothing. It is ongoing noise, not a breach — but see the
actions below, because the *exposure* (not the outcome) is what needs closing.

## Scale

- **293 unique source IPs**
- **~20,715** secret-scanning requests in 6 days
- The host's raw origin IP `98.14.10.98` is hit **directly** on 80/443
  (scanners send the CT-log hostname as a `Host` header and bypass Cloudflare
  entirely) — 1,254 raw-IP hits in the scan set alone.

## Targets (by hostname in the request)

| Host | Hits |
|---|---:|
| ntfy.incoming.nyc | 9326 |
| zipline.incoming.nyc | 2021 |
| refine.shows.pics | 1704 |
| kuma.incoming.nyc | 1692 |
| **98.14.10.98 (raw IP)** | **1254** |
| bitwarden.incoming.nyc | 1081 |
| qwen.incoming.nyc | 976 |
| plaid.delilah.incoming.nyc | 921 |
| omniroute.incoming.nyc | 843 |
| incoming.nyc (apex) | 542 |
| stremio.incoming.nyc | 240 |
| concierge.incoming.nyc | 101 |
| pterodactyl / couch / ai / tasks / pdf / neovim.incoming.nyc | 1–3 each |

## Signatures

Top user-agents (all spoofed — no real browser emits these):

- `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) … Chrome/131.0.0.0` — 11,514
- `Mozilla/5.0 (Windows NT 10.0; Win64; x64) … Chrome/131.0.0.0` — 3,102
- `Mozilla/5.0 (X11; Linux x86_64) … Chrome/131.0.0.0` — 2,298
- `Mozilla/5.0 … (… Yokohama Institute of Information Security https://www.iisec.ac.jp) Chrome/124.0.0.0` — 260 (fabricated UA using a real university's name)
- `l9explore/1.2.2` — 95 (**Censys** scanner)
- `Mozilla/5.0 (compatible; SecurityResearch/1.0)` — 35
- `Mozilla/5.0 (compatible; Googlebot/2.1; …)` — 167 (Googlebot spoof)
- `Go-http-client/1.1` — 83

One scanner leaks its own IP in the UA string: `… Safari/537.36 [ip:93.41.125.186]`
(291 hits).

## Attribution

The bulk of the fleet reverse-resolves to `*.bc.googleusercontent.com` — **Google
Cloud** instances, each firing an identical ~246-request sweep (clear sign of a
distributed commercial scanner). A second group arrives via **Cloudflare edge IPs**
(`172.6x`/`172.7x`/`104.2x`/`162.15x`/`141.101.x`/`108.162.x`) — those are *not*
attacker IPs, they are the Cloudflare proxy hiding the real client.

### Real (non-Cloudflare, non-GCP) sources — act on these

| IP | Hits | Notes |
|---|---:|---|
| 213.209.159.154 | 320 | Bulgaria, hits origin directly |
| 94.154.46.246 | 160 | hits origin directly |
| 45.67.211.147 | 150 | hits origin directly (671 incl. all paths) |
| 46.62.170.251 | 149 | hits origin directly (653) |
| 45.138.12.29 | 105 | |
| 45.148.10.5 | 104 | hits origin directly; also `.120`, `.21` |
| 34.19.86.210 | 149 | GCP, hits origin directly |
| 193.32.204.199 | 95 | hits origin directly; also `.162.154/.155` |
| 43.156.77.155 | 55 | hits origin directly |
| 161.118.208.28 | 7 | hits origin directly |
| 89.126.211.166 | 47 | hits origin directly |
| 34.154.52.181 | **9300** | GCP — single most aggressive source |
| 8.234.56.224 | 738 | GCP |
| 34.50.12.235 | 492 | GCP |
| 93.41.125.186 | 291 | self-reported in UA |

### GCP fleet (uniform ~246-hit sweeps)

`8.235.53.100, 8.234.93.213, 8.234.56.95, 8.234.36.182, 34.48.138.62,
34.40.196.68, 34.40.190.182, 34.39.98.29, 34.35.51.138, 34.158.31.253,
34.158.216.236, 34.154.48.191, 34.140.132.132, 34.50.162.241, 34.147.220.252,
34.59.173.211, 34.62.116.145, 34.52.229.253, 34.156.22.222, 34.156.121.46,
34.52.133.111, 34.79.124.68, 34.21.50.215, 34.50.75.89, 35.229.243.49,
34.12.128.52, 35.232.81.147, 35.224.74.159, 35.185.173.105, 34.12.137.1`

### Other VPS / hosting ranges seen

DigitalOcean (`157.245.113.227, 159.89.127.165, 139.59.231.238, 139.59.132.8,
134.209.112.100, 64.227.70.2, 206.189.95.232, 209.38.248.17, 207.154.212.47`),
AWS (`3.82.141.143, 13.200.6.88, 16.5.0.236`), Oracle (`129.213.151.234`),
plus `185.218.86.24, 185.93.89.64, 176.65.148.71, 91.92.241.196, 91.148.245.81,
85.239.151.78, 85.203.46.173, 80.94.92.126, 2.57.122.202, 102.220.161.102/64,
144.172.93.238/89.127/109.237, 154.54.100.176/179, 216.81.248.147,
204.76.203.31, 64.89.160.19, 104.243.33.53, 45.115.26.203, 172.86.81.177/140`.

## Recommended actions

1. **Stop exposing the origin.** `98.14.10.98:80/443` is directly reachable and
   bypasses Cloudflare. Restrict 80/443 on the router/host to Cloudflare's
   published ranges only (`https://www.cloudflare.com/ips-v4`), or put the host
   behind a Cloudflare Tunnel so it takes no inbound internet traffic.
2. **Stop publishing the bot API.** `plaid.delilah.incoming.nyc` and
   `concierge.incoming.nyc` both forward to `192.168.0.167:8000` — the
   delilah_bot FastAPI. That is why the bot's uvicorn log is full of scanner
   traffic. Put NPM access control (or Cloudflare Access) in front of those two
   hosts, or drop them entirely if they don't need public reachability.
3. **Close the direct path to the container.** `delilah_bot` maps
   `0.0.0.0:8000->8000`; confirm the router is not also forwarding `8000`
   straight to it, and bind the mapping to `127.0.0.1` if only NPM needs it.
4. **Log the real client IP.** For Cloudflare-fronted hosts, add
   `CF-Connecting-IP` / `X-Forwarded-For` to the NPM log format (or enable
   real-IP restore). Right now the Cloudflare-proxied hits record only the CF edge.
5. **Block the direct-origin offenders** at NPM/host firewall:
   `213.209.159.154, 94.154.46.246, 45.67.211.147, 46.62.170.251, 45.148.10.0/24,
   193.32.204.199, 45.138.12.29, 43.156.77.155, 89.126.211.166, 34.154.52.181`
   and fail2ban the rest by pattern.
