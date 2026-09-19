# Azure FYERS deployment

The VM endpoint is `shivank@20.198.86.122`. Keep TCP 8000 closed in the Azure
NSG; restrict TCP 22 to the operator's trusted source IP.

1. Clone to `/home/shivank/Crypto-trader`, create `.venv312`, install
   `requirements-lock.txt`, and set `.env` mode to `0600`.
2. Verify `curl -4 https://ifconfig.me` returns `20.198.86.122` and that this is
   the static IP registered with FYERS.
3. Copy the units to `/etc/systemd/system/`, then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fyers-dashboard.service fyers-backup.timer
systemctl status fyers-dashboard.service
journalctl -u fyers-dashboard.service -f
```

Connect from the operator machine:

```bash
ssh -L 8000:127.0.0.1:8000 shivank@20.198.86.122
```

Open `http://127.0.0.1:8000`. Renew the FYERS OAuth token explicitly when the
dashboard reports account errors. Test a backup without replacing production:

```bash
.venv312/bin/python main.py fyers restore-drill \
  --backup data/fyers/backups/FILE.sqlite3 --output-directory /tmp/fyers-restore-drill
```

Live execution remains disabled. Installing these services is an explicit VM
operator action; repository code does not modify Azure networking or systemd.
