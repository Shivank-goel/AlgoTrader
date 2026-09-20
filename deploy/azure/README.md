# Azure FYERS deployment

The VM endpoint is `shivank@20.198.86.122`. Keep TCP 8000 closed in the Azure
NSG; restrict TCP 22 to the operator's trusted source IP.

Before deployment, rotate the FYERS app secret that was exposed during setup and
put only the replacement in `.env`; the installer copies it outside the checkout.

1. Clone the repository and run the idempotent installer. It detects the checkout
   path, requires Python 3.12, creates `.venv312`, copies secrets to
   `/etc/algotrader/fyers.env`, installs all units and smoke-tests the API:

```bash
cd /home/shivank/AlgoTrader
sudo deploy/azure/install.sh
```

After the first successful install, remove the checkout `.env` manually; reruns
use `/etc/algotrader/fyers.env` unless `--refresh-env` is explicitly requested.
2. Verify `curl -4 https://ifconfig.me` returns `20.198.86.122` and that this is
   the static IP registered with FYERS.
3. Copy the units to `/etc/systemd/system/`, then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fyers-dashboard.service fyers-backup.timer fyers-health.timer
systemctl status fyers-dashboard.service
journalctl -u fyers-dashboard.service -f
```

Connect from the operator machine:

```bash
ssh -L 8000:127.0.0.1:8000 shivank@20.198.86.122
```

Open `http://127.0.0.1:8000`. Renew the FYERS OAuth token explicitly when the
dashboard reports `AUTH_REQUIRED`; then refresh the protected environment with
`sudo deploy/azure/install.sh --refresh-env`. Test a backup without replacing production:

```bash
.venv312/bin/python main.py fyers restore-drill \
  --backup data/fyers/backups/FILE.sqlite3 --output-directory /tmp/fyers-restore-drill
```

Live execution remains disabled. Installing these services is an explicit VM
operator action; repository code does not modify Azure networking or systemd.

Configure Azure Monitor and Azure Backup in the Azure portal: VM availability,
missing heartbeat, failed service/preflight logs, disk pressure and backup failure
must reach an email action group. Enable VM/disk backup and restore into an isolated
VM. Repository code cannot activate these subscription-owned resources safely.
