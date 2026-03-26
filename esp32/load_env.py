Import("env")

from pathlib import Path

envfile = Path(env.subst("$PROJECT_DIR")) / ".env"
if not envfile.exists():
    print("WARNING: esp32/.env not found — create it with WIFI_SSID and WIFI_PASS")
else:
    for line in envfile.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key in ("WIFI_SSID", "WIFI_PASS"):
            env.Append(CPPDEFINES=[(key, env.StringifyMacro(value))])
