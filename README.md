# Airmon

## Service installation (as `root`)

```sh
cp airmon.service /etc/systemd/system/
systemctl enable airmon
systemctl start airmon
```

To check status:
```sh
systemctl status airmon
```