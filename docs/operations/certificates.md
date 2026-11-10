# HTTPS certificates: inventory, monitoring, renewal and recovery

The monitor is served over HTTPS by nginx on the central host (`deploy/nginx/labmon.conf`). The
API itself listens on 127.0.0.1:8000 only. Browsers and collectors must trust the certificate
and the host name it names. This page covers the site-issued certificate. Evidence: the expiry
check is tested against real TLS servers with certificates from a throwaway CA
(`tests/unit/test_tls.py`: valid, expiring, expired, wrong host, untrusted). A renewal has not
yet been staged.

## Inventory

| Item | Where | Owner | Readable by |
| --- | --- | --- | --- |
| Server certificate + chain | `/etc/labmon/tls/fullchain.pem` | site PKI / IT | root, nginx |
| Server private key | `/etc/labmon/tls/privkey.pem` (0600) | site PKI / IT | root, nginx only |
| Site CA bundle (if not in the system store) | `/etc/labmon/tls/site-ca.pem` | site PKI | everyone |
| Collector trust (Linux) | `ca_file` in `/etc/labmon/collector/collector.toml` | administrator | collector service |
| Collector trust (Windows) | `ca_file` in `%ProgramData%\labmon\collector\collector.toml` | administrator | LocalService |

The private key never leaves the central host. It is not part of any release bundle, backup
dump or collector package.

## Monitoring

Set `LABMON_TLS_CHECK_URL` in `/etc/labmon/labmon.env` to the URL clients use, for example
`https://monitor.lab.example`. Add `LABMON_TLS_CA_FILE` if the site CA is not in the system store.
The check performs a real handshake and validates the chain and host name, as a browser would. It
reports the expiry date:

- `labmon-server doctor` shows **tls certificate**: ✓ valid, `!` under 30 days, ✕ rejected
  (expired, untrusted or wrong host).
- **Diagnostics → Central service** shows the same result, cached for 5 min. Faults are raised
  as *warning* under 30 days and *critical* when the certificate is rejected.
- Without `LABMON_TLS_CHECK_URL`, production shows a warning that expiry is not monitored.

Run the check from the central host. The host must resolve the public name, or the hosts file
must map it to the local address. Also verify once from a client workstation after every change
(below).

## Renewal (before expiry; routine)

1. Request a certificate for the exact public name, plus any aliases users type, from the site
   CA. Use a new key unless policy says otherwise. Keep the CSR and the key on the central host.
2. Install the new files beside the old ones, then switch:

   ```bash
   sudo install -m 0644 new-fullchain.pem /etc/labmon/tls/fullchain.pem.new
   sudo install -m 0600 new-privkey.pem   /etc/labmon/tls/privkey.pem.new
   sudo openssl x509 -in /etc/labmon/tls/fullchain.pem.new -noout -subject -enddate -ext subjectAltName
   sudo mv /etc/labmon/tls/fullchain.pem.new /etc/labmon/tls/fullchain.pem
   sudo mv /etc/labmon/tls/privkey.pem.new /etc/labmon/tls/privkey.pem
   sudo nginx -t && sudo systemctl reload nginx      # reload: open connections are not dropped
   ```

3. Verify: run `labmon-server doctor` (tls certificate ✓ with the new expiry date). From a
   client workstation, open the monitor in a browser and check there is no warning. Also run:

   ```bash
   curl --cacert site-ca.pem -sS -o /dev/null -w '%{http_code}\n' https://monitor.lab.example/health/ready
   ```

4. Collectors need no change when the issuing CA is unchanged. Check **Collectors**: every
   collector stays online and has no upload fault.

Keep the old certificate and key until step 3 passes. To undo, move them back and reload nginx.

## CA change (rare; planned)

When the site moves to a new issuing CA, collectors must trust it **before** the server
certificate changes. Collectors read a single `ca_file`, so give them a bundle that holds both
the old and the new CA:

1. `cat old-ca.pem new-ca.pem > site-ca.pem`. Place it as each collector's `ca_file`, restart the
   collector and confirm it is online.
2. Install the server certificate from the new CA (renewal steps 2–3).
3. After all collectors are confirmed, remove the old CA from the bundle in a later change.

## When a certificate has expired or is rejected (incident)

What happens:

- Browsers show a certificate warning. Users must not click through it; report it instead.
- Collectors cannot upload. They record `network: ConnectError` attempts, retry with backoff and
  **keep spooling**, so nothing is lost while the spool has capacity. Size `spool_capacity` for at
  least 24 h at the declared peak rate (SPECIFICATIONS §10); the default of 500,000 records is
  not proven to cover that for every fleet. `labmon-collector status` shows *pending* growing.
- Readings stay safe on the instrument PCs. The UI shows the instruments as stale or the
  collectors as offline, which is the correct display.

Recovery: install a valid certificate (renewal steps 2–3). Collectors reconnect on their next
retry, at most `max_backoff_s` (60 s by default) later, and drain the backlog. History has no
hole, provided the spool did not fill. Confirm on **Collectors** and with `labmon-collector status`
(*pending* back to near zero). Record the incident: start, end, the peak backlog, and whether any
spool reached capacity.

## Never

- Never disable certificate verification on collectors or in scripts (`allow_insecure_http` is
  for development over plain HTTP only and is refused in production packages).
- Never copy the private key into a ticket, chat, release bundle or collector package.
