import os
import json
import secrets
import string
import hashlib
import shutil
import sqlite3
import ssl
import time
import urllib.request
import subprocess
from pathlib import Path
from typing import Optional

import docker
from fastapi import FastAPI, HTTPException, Header, Depends, Response, Cookie, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# Load environment variables from .env if present
try:
    from dotenv import load_dotenv
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        load_dotenv(dotenv_path=env_file)
except ImportError:
    pass

# Base configuration
BASE_DIR = Path(os.getenv("FLEET_BASE_DIR", Path(__file__).resolve().parent.parent))
DATA_FILE = BASE_DIR / "panel" / "data" / "fleet.json"
ADMIN_CRED_FILE = BASE_DIR / "panel" / "data" / "admin.json"
NGINX_CONF_DIR = BASE_DIR / "nginx" / "conf.d"
USERS_DIR = BASE_DIR / "users"

RAW_DOMAIN_NAME = os.getenv("DOMAIN_NAME", "").strip()
if RAW_DOMAIN_NAME.lower() in ("example.com", "localhost", "none", "null"):
    DOMAIN_NAME = ""
else:
    DOMAIN_NAME = RAW_DOMAIN_NAME

SERVER_IP = os.getenv("SERVER_IP", "").strip()

PORT_RANGE_START = int(os.getenv("PORT_RANGE_START", "21001"))
PORT_RANGE_END = int(os.getenv("PORT_RANGE_END", "21050"))
DOCKER_NETWORK = os.getenv("DOCKER_NETWORK", "9router-fleet-net")
ROUTER_IMAGE = os.getenv("ROUTER_IMAGE", "decolua/9router:latest")

CONTAINER_MEM_LIMIT = os.getenv("CONTAINER_MEM_LIMIT", "256m")
CONTAINER_MEM_RESERVATION = os.getenv("CONTAINER_MEM_RESERVATION", "96m")
CONTAINER_CPUS = float(os.getenv("CONTAINER_CPUS", "0.35"))
NANO_CPUS = int(CONTAINER_CPUS * 1_000_000_000)

ENABLE_UPSTREAM_PROXY = os.getenv("ENABLE_UPSTREAM_PROXY", "true").lower() in ("true", "1", "yes")
UPSTREAM_PROXY_NAME = os.getenv("UPSTREAM_PROXY_NAME", "Warp")
UPSTREAM_PROXY_URL = os.getenv("UPSTREAM_PROXY_URL", "http://172.18.0.1:40001")
UPSTREAM_PROXY_TYPE = os.getenv("UPSTREAM_PROXY_TYPE", "http")
UPSTREAM_PROXY_STRICT = os.getenv("UPSTREAM_PROXY_STRICT", "false").lower() in ("true", "1", "yes")

SSL_CERT_PATH = os.getenv("SSL_CERT_PATH", f"/etc/letsencrypt/live/{DOMAIN_NAME}/fullchain.pem" if DOMAIN_NAME else "")
SSL_KEY_PATH = os.getenv("SSL_KEY_PATH", f"/etc/letsencrypt/live/{DOMAIN_NAME}/privkey.pem" if DOMAIN_NAME else "")

def is_ssl_active() -> bool:
    if not DOMAIN_NAME or not SSL_CERT_PATH or not SSL_KEY_PATH:
        return False
    return Path(SSL_CERT_PATH).exists() and Path(SSL_KEY_PATH).exists()

def get_instance_urls(port: int, request: Optional[Request] = None):
    ssl_enabled = is_ssl_active()
    scheme = "https" if ssl_enabled else "http"
    if DOMAIN_NAME:
        host = DOMAIN_NAME
    elif request and request.headers.get("host"):
        # Uses whatever IP or hostname you typed in your browser address bar
        host = request.headers.get("host").split(":")[0]
    elif SERVER_IP:
        host = SERVER_IP
    else:
        host = "localhost"
    return {
        "base_url": f"{scheme}://{host}:{port}/v1",
        "panel_url": f"{scheme}://{host}:{port}"
    }

app = FastAPI(title="9Router Fleet Manager")
client = docker.from_env()

def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()

def init_admin():
    ADMIN_CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not ADMIN_CRED_FILE.exists():
        admin_user = "admin_" + secrets.token_hex(4)
        admin_pass = "FleetSec_" + secrets.token_urlsafe(18)
        salt = secrets.token_hex(16)
        pw_hash = hash_pw(admin_pass, salt)
        data = {
            "username": admin_user,
            "salt": salt,
            "password_hash": pw_hash,
            "plain_initial_pass": admin_pass,
            "sessions": {}
        }
        with open(ADMIN_CRED_FILE, "w") as f:
            json.dump(data, f, indent=2)

init_admin()

def load_admin():
    with open(ADMIN_CRED_FILE, "r") as f:
        return json.load(f)

def save_admin(data):
    with open(ADMIN_CRED_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_db():
    if not DATA_FILE.exists():
        return {}
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_db(data):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def generate_password(length=24):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))

def inject_default_proxy(user_data_dir: Path):
    if not ENABLE_UPSTREAM_PROXY:
        return

    import datetime
    db_file = user_data_dir / "db" / "data.sqlite"
    # Wait up to 45 seconds for 9router container to start, create sqlite db and run migrations
    for _ in range(90):
        if db_file.exists() and db_file.stat().st_size > 0:
            break
        time.sleep(0.5)

    if not db_file.exists() or db_file.stat().st_size == 0:
        print(f"inject_default_proxy: {db_file} not ready after wait")
        return

    # Retry SQLite connection in case 9router container has WAL lock during migration
    for attempt in range(10):
        try:
            conn = sqlite3.connect(str(db_file), timeout=15.0)
            c = conn.cursor()
            c.execute("CREATE TABLE IF NOT EXISTS proxyPools (id TEXT PRIMARY KEY, isActive INTEGER DEFAULT 1, testStatus TEXT, data TEXT NOT NULL, createdAt TEXT NOT NULL, updatedAt TEXT NOT NULL);")
            c.execute("SELECT id FROM proxyPools WHERE id = 'upstream-fleet-proxy';")
            if not c.fetchone():
                now = datetime.datetime.utcnow().isoformat() + "Z"
                data_json = json.dumps({
                    "name": UPSTREAM_PROXY_NAME,
                    "proxyUrl": UPSTREAM_PROXY_URL,
                    "noProxy": "",
                    "type": UPSTREAM_PROXY_TYPE,
                    "strictProxy": UPSTREAM_PROXY_STRICT
                })
                c.execute("INSERT INTO proxyPools (id, isActive, testStatus, data, createdAt, updatedAt) VALUES (?, ?, ?, ?, ?, ?);",
                          ("upstream-fleet-proxy", 1, "active", data_json, now, now))
                conn.commit()
                print(f"inject_default_proxy: successfully injected {UPSTREAM_PROXY_NAME} into {db_file}")
            conn.close()
            break
        except Exception as e:
            print(f"Attempt {attempt+1} failed to inject proxy pool: {e}")
            time.sleep(1)

def get_next_available_port():
    db = load_db()
    used_ports = {item.get("port") for item in db.values() if "port" in item}
    for p in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        if p not in used_ports:
            return p
    raise HTTPException(status_code=400, detail=f"No free ports available in range {PORT_RANGE_START}-{PORT_RANGE_END}")

def update_nginx_config(username: str, port: int, container_name: str):
    ssl_active = is_ssl_active()
    server_name_val = DOMAIN_NAME if DOMAIN_NAME else "_"

    if ssl_active:
        listen_block = f"""    listen {port} ssl;
    server_name {server_name_val};

    ssl_certificate {SSL_CERT_PATH};
    ssl_certificate_key {SSL_KEY_PATH};
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;"""
        proto_header = "https"
    else:
        listen_block = f"""    listen {port};
    server_name {server_name_val};"""
        proto_header = "$scheme"

    conf_content = f"""# User: {username}
server {{
{listen_block}

    client_max_body_size 128M;

    location /api/auth {{
        limit_req zone=auth_limit burst=5 nodelay;
        proxy_pass http://{container_name}:20128;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto {proto_header};
    }}

    location / {{
        proxy_pass http://{container_name}:20128;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto {proto_header};
        proxy_buffering off;
        proxy_read_timeout 600s;
    }}
}}
"""
    NGINX_CONF_DIR.mkdir(parents=True, exist_ok=True)
    conf_file = NGINX_CONF_DIR / f"{username}.conf"
    with open(conf_file, "w") as f:
        f.write(conf_content)
    try:
        guard = client.containers.get("9router-guardian")
        res = guard.exec_run("nginx -t")
        if res.exit_code == 0:
            guard.exec_run("nginx -s reload")
        else:
            print(f"Nginx config test warning: {res.output.decode('utf-8', errors='ignore')}")
    except Exception as e:
        print(f"Nginx reload note: {e}")

def remove_nginx_config(username: str):
    conf_file = NGINX_CONF_DIR / f"{username}.conf"
    if conf_file.exists():
        conf_file.unlink()
    try:
        guard = client.containers.get("9router-guardian")
        guard.exec_run("nginx -s reload")
    except Exception:
        pass

def sync_all_nginx_configs():
    try:
        db = load_db()
        for username, info in db.items():
            port = info.get("port")
            c_name = info.get("container_name")
            if port and c_name:
                update_nginx_config(username, port, c_name)
    except Exception as e:
        print(f"Startup Nginx sync note: {e}")

sync_all_nginx_configs()

def verify_session(fleet_session: Optional[str] = Cookie(None), x_admin_token: Optional[str] = Header(None)):
    admin_data = load_admin()
    fleet_admin_token = os.getenv("FLEET_ADMIN_TOKEN")
    if fleet_admin_token and x_admin_token == fleet_admin_token:
        return "admin"
    if x_admin_token and x_admin_token in admin_data.get("sessions", {}):
        return admin_data["username"]
    if fleet_session and fleet_session in admin_data.get("sessions", {}):
        return admin_data["username"]
    raise HTTPException(status_code=401, detail="Unauthorized")

def _rollback(c_name: str, old_c_temp_name: str, db_file: Path, bak_file: Path):
    # 1. Restore SQLite DB and clean wal/shm
    if bak_file.exists():
        try:
            wal = db_file.parent / (db_file.name + "-wal")
            shm = db_file.parent / (db_file.name + "-shm")
            if wal.exists():
                try: wal.unlink()
                except Exception: pass
            if shm.exists():
                try: shm.unlink()
                except Exception: pass
            shutil.copy2(str(bak_file), str(db_file))
            bak_file.unlink()
            print(f"Rollback: Restored SQLite database from {bak_file}")
        except Exception as e:
            print(f"Rollback error restoring DB: {e}")

    # 2. Restore and restart old container
    try:
        old_c = client.containers.get(old_c_temp_name)
        old_c.rename(c_name)
        old_c.start()
        print(f"Rollback: Restored and started old container {c_name}")
    except Exception as e:
        print(f"Rollback error restoring container: {e}")

class LoginReq(BaseModel):
    username: str
    password: str

class CreateUserReq(BaseModel):
    username: str
    port: Optional[int] = None
    initial_password: Optional[str] = None
    memo: Optional[str] = None

class UpdateUserReq(BaseModel):
    new_username: Optional[str] = None
    memo: Optional[str] = None
    new_password: Optional[str] = None

@app.post("/api/login")
def login(req: LoginReq, response: Response):
    admin_data = load_admin()
    if req.username != admin_data.get("username"):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    expected_hash = admin_data.get("password_hash")
    salt = admin_data.get("salt")
    if hash_pw(req.password, salt) != expected_hash:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    session_token = secrets.token_hex(24)
    admin_data.setdefault("sessions", {})[session_token] = True
    save_admin(admin_data)

    response.set_cookie(key="fleet_session", value=session_token, httponly=True, max_age=86400 * 30)
    return {"success": True, "token": session_token, "username": admin_data["username"]}

@app.post("/api/logout")
def logout(response: Response, fleet_session: Optional[str] = Cookie(None)):
    if fleet_session:
        admin_data = load_admin()
        if fleet_session in admin_data.get("sessions", {}):
            del admin_data["sessions"][fleet_session]
            save_admin(admin_data)
    response.delete_cookie(key="fleet_session")
    return {"success": True}

@app.get("/api/auth-check")
def auth_check(user=Depends(verify_session)):
    return {"status": "ok", "user": user}

@app.get("/api/instances")
def list_instances(request: Request, user=Depends(verify_session)):
    db = load_db()
    results = []
    for username, info in db.items():
        c_name = info.get("container_name")
        status = "stopped"
        try:
            c = client.containers.get(c_name)
            status = c.status
        except docker.errors.NotFound:
            status = "not_found"
        except Exception:
            status = "error"

        port = info.get("port")
        urls = get_instance_urls(port, request)
        results.append({
            "username": username,
            "port": port,
            "container_name": c_name,
            "initial_password": info.get("initial_password"),
            "status": status,
            "version": info.get("version", "latest"),
            "created_at": info.get("created_at"),
            "memo": info.get("memo", ""),
            "base_url": urls["base_url"],
            "panel_url": urls["panel_url"]
        })
    return results

@app.post("/api/instances")
def create_instance(req: CreateUserReq, request: Request, user=Depends(verify_session)):
    username = req.username.strip().lower()
    if not username.isalnum() or len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be alphanumeric and >= 3 chars")

    db = load_db()
    if username in db:
        raise HTTPException(status_code=400, detail="Username already exists")

    port = req.port or get_next_available_port()
    password = req.initial_password or generate_password(24)
    jwt_secret = generate_password(32)
    container_name = f"9router-{username}"

    user_data_dir = USERS_DIR / username / "data"
    user_data_dir.mkdir(parents=True, exist_ok=True)
    try:
        # Give container non-root UID 1000 permissions
        subprocess.run(["chown", "-R", "1000:1000", str(USERS_DIR / username)], check=False)
    except Exception:
        pass

    try:
        try:
            old = client.containers.get(container_name)
            old.remove(force=True)
        except docker.errors.NotFound:
            pass

        client.containers.run(
            image=ROUTER_IMAGE,
            name=container_name,
            detach=True,
            restart_policy={"Name": "unless-stopped"},
            network=DOCKER_NETWORK,
            volumes={
                str(user_data_dir): {"bind": "/app/data", "mode": "rw"}
            },
            environment={
                "NODE_ENV": "production",
                "PORT": "20128",
                "HOSTNAME": "0.0.0.0",
                "DATA_DIR": "/app/data",
                "INITIAL_PASSWORD": password,
                "JWT_SECRET": jwt_secret,
            },
            mem_limit=CONTAINER_MEM_LIMIT,
            mem_reservation=CONTAINER_MEM_RESERVATION,
            nano_cpus=NANO_CPUS
        )

        update_nginx_config(username, port, container_name)

        # Inject default proxy pool into instance sqlite db in background if enabled
        if ENABLE_UPSTREAM_PROXY:
            import threading
            threading.Thread(target=inject_default_proxy, args=(user_data_dir,), daemon=True).start()

        import datetime
        db[username] = {
            "username": username,
            "port": port,
            "container_name": container_name,
            "initial_password": password,
            "memo": req.memo or "",
            "version": "latest",
            "created_at": datetime.datetime.utcnow().isoformat()
        }
        save_db(db)

        urls = get_instance_urls(port, request)
        return {
            "success": True,
            "username": username,
            "port": port,
            "initial_password": password,
            "base_url": urls["base_url"],
            "panel_url": urls["panel_url"]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.patch("/api/instances/{username}")
def update_instance(username: str, req: UpdateUserReq, user=Depends(verify_session)):
    db = load_db()
    if username not in db:
        raise HTTPException(status_code=404, detail="Instance not found")

    info = db[username]
    target_username = username

    # Handle username rename
    if req.new_username and req.new_username.strip().lower() != username:
        new_u = req.new_username.strip().lower()
        if not new_u.isalnum() or len(new_u) < 3:
            raise HTTPException(status_code=400, detail="New username must be alphanumeric and >= 3 chars")
        if new_u in db:
            raise HTTPException(status_code=400, detail="New username already exists")

        old_data_dir = USERS_DIR / username
        new_data_dir = USERS_DIR / new_u
        if old_data_dir.exists():
            old_data_dir.rename(new_data_dir)

        remove_nginx_config(username)

        old_c_name = info["container_name"]
        try:
            old_c = client.containers.get(old_c_name)
            old_c.remove(force=True)
        except Exception:
            pass

        target_username = new_u
        info["username"] = new_u
        info["container_name"] = f"9router-{new_u}"
        del db[username]
        db[new_u] = info

    if req.memo is not None:
        info["memo"] = req.memo

    if req.new_password:
        info["initial_password"] = req.new_password

    # Recreate container to apply changes
    port = info["port"]
    c_name = info["container_name"]
    user_data_dir = USERS_DIR / target_username / "data"
    jwt_secret = generate_password(32)

    try:
        try:
            c = client.containers.get(c_name)
            c.remove(force=True)
        except Exception:
            pass

        client.containers.run(
            image=ROUTER_IMAGE,
            name=c_name,
            detach=True,
            restart_policy={"Name": "unless-stopped"},
            network=DOCKER_NETWORK,
            volumes={
                str(user_data_dir): {"bind": "/app/data", "mode": "rw"}
            },
            environment={
                "NODE_ENV": "production",
                "PORT": "20128",
                "HOSTNAME": "0.0.0.0",
                "DATA_DIR": "/app/data",
                "INITIAL_PASSWORD": info["initial_password"],
                "JWT_SECRET": jwt_secret,
            },
            mem_limit=CONTAINER_MEM_LIMIT,
            mem_reservation=CONTAINER_MEM_RESERVATION,
            nano_cpus=NANO_CPUS
        )
        update_nginx_config(target_username, port, c_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to rebuild container: {str(e)}")

    save_db(db)
    return {"success": True, "info": info}

@app.post("/api/instances/{username}/sync-proxy")
def sync_instance_proxy(username: str, user=Depends(verify_session)):
    if not ENABLE_UPSTREAM_PROXY:
        raise HTTPException(status_code=400, detail="Upstream proxy injection is disabled in configuration")

    db = load_db()
    if username not in db:
        raise HTTPException(status_code=404, detail="Instance not found")
    user_data_dir = USERS_DIR / username / "data"
    db_file = user_data_dir / "db" / "data.sqlite"
    if not db_file.exists():
        raise HTTPException(status_code=400, detail="Instance database file not initialized yet")

    import datetime
    try:
        conn = sqlite3.connect(str(db_file), timeout=10.0)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS proxyPools (id TEXT PRIMARY KEY, isActive INTEGER DEFAULT 1, testStatus TEXT, data TEXT NOT NULL, createdAt TEXT NOT NULL, updatedAt TEXT NOT NULL);")
        c.execute("DELETE FROM proxyPools WHERE id = 'upstream-fleet-proxy';")
        now = datetime.datetime.utcnow().isoformat() + "Z"
        data_json = json.dumps({
            "name": UPSTREAM_PROXY_NAME,
            "proxyUrl": UPSTREAM_PROXY_URL,
            "noProxy": "",
            "type": UPSTREAM_PROXY_TYPE,
            "strictProxy": UPSTREAM_PROXY_STRICT
        })
        c.execute("INSERT INTO proxyPools (id, isActive, testStatus, data, createdAt, updatedAt) VALUES (?, ?, ?, ?, ?, ?);",
                  ("upstream-fleet-proxy", 1, "active", data_json, now, now))
        conn.commit()
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    c_name = db[username].get("container_name")
    try:
        c = client.containers.get(c_name)
        c.restart()
    except Exception:
        pass

    return {"success": True, "message": f"{UPSTREAM_PROXY_NAME} outbound proxy pool successfully injected and container restarted."}

@app.post("/api/instances/{username}/action")
def instance_action(username: str, action: str, user=Depends(verify_session)):
    db = load_db()
    if username not in db:
        raise HTTPException(status_code=404, detail="Instance not found")
    c_name = db[username].get("container_name")
    try:
        c = client.containers.get(c_name)
        if action == "restart":
            c.restart()
        elif action == "stop":
            c.stop()
        elif action == "start":
            c.start()
        else:
            raise HTTPException(status_code=400, detail="Unknown action")
        return {"success": True, "action": action, "status": c.status}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/instances/{username}/update")
def update_instance_version(username: str, user=Depends(verify_session)):
    db = load_db()
    if username not in db:
        raise HTTPException(status_code=404, detail="Instance not found")

    info = db[username]
    c_name = info.get("container_name", f"9router-{username}")
    port = info.get("port")
    user_data_dir = USERS_DIR / username / "data"
    db_file = user_data_dir / "db" / "data.sqlite"
    bak_file = user_data_dir / "db" / "data.sqlite.pre_update_bak"

    # 1. Verify container exists and grab env / specs
    try:
        old_c = client.containers.get(c_name)
    except docker.errors.NotFound:
        raise HTTPException(status_code=400, detail=f"Container {c_name} not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Docker error: {str(e)}")

    env_dict = dict(e.split("=", 1) for e in old_c.attrs.get("Config", {}).get("Env", []) if "=" in e)
    jwt_secret = env_dict.get("JWT_SECRET") or generate_password(32)
    initial_pass = info.get("initial_password") or env_dict.get("INITIAL_PASSWORD", "")

    # 2. Pre-pull latest image before touching running container
    try:
        img_parts = ROUTER_IMAGE.split(":")
        repo = img_parts[0]
        tag = img_parts[1] if len(img_parts) > 1 else "latest"
        client.images.pull(repo, tag=tag)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to pull latest 9router image: {str(e)}")

    # 3. Create atomic SQLite backup
    if db_file.exists():
        try:
            src = sqlite3.connect(str(db_file), timeout=15.0)
            dst = sqlite3.connect(str(bak_file))
            src.backup(dst)
            dst.close()
            src.close()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Pre-update DB backup failed: {str(e)}")

    # 4. Keep warm fallback: stop and rename old container
    old_c_temp_name = f"{c_name}-old"
    try:
        try:
            leftover = client.containers.get(old_c_temp_name)
            leftover.remove(force=True)
        except docker.errors.NotFound:
            pass

        old_c.stop(timeout=5)
        old_c.rename(old_c_temp_name)
    except Exception as e:
        if bak_file.exists():
            try: bak_file.unlink()
            except Exception: pass
        raise HTTPException(status_code=500, detail=f"Failed to prepare container for update: {str(e)}")

    # 5. Launch new container
    new_c = None
    try:
        new_c = client.containers.run(
            image=ROUTER_IMAGE,
            name=c_name,
            detach=True,
            restart_policy={"Name": "unless-stopped"},
            network=DOCKER_NETWORK,
            volumes={
                str(user_data_dir): {"bind": "/app/data", "mode": "rw"}
            },
            environment={
                "NODE_ENV": "production",
                "PORT": "20128",
                "HOSTNAME": "0.0.0.0",
                "DATA_DIR": "/app/data",
                "INITIAL_PASSWORD": initial_pass,
                "JWT_SECRET": jwt_secret,
            },
            mem_limit=CONTAINER_MEM_LIMIT,
            mem_reservation=CONTAINER_MEM_RESERVATION,
            nano_cpus=NANO_CPUS
        )
    except Exception as e:
        print(f"Failed to run new container for {username}: {e}. Initiating rollback...")
        _rollback(c_name, old_c_temp_name, db_file, bak_file)
        raise HTTPException(status_code=500, detail=f"Container launch failed: {str(e)}. Automatically rolled back.")

    # 6. Health check loop (Next.js may take 15-25s on first boot to render /login)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    health_ok = False
    health_err = ""
    time.sleep(3)
    for attempt in range(16):
        try:
            new_c.reload()
            if new_c.status != "running":
                health_err = f"Container stopped unexpectedly with status: {new_c.status}"
                break
        except Exception as e:
            health_err = f"Container status check failed: {str(e)}"
            break

        proto = "https" if is_ssl_active() else "http"
        url = f"{proto}://127.0.0.1:{port}/login"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FleetManager/1.0"})
            url_ctx = ctx if proto == "https" else None
            with urllib.request.urlopen(req, context=url_ctx, timeout=3) as resp:
                if resp.status == 200:
                    health_ok = True
                    break
        except Exception as e:
            health_err = str(e)
            time.sleep(2)

    # 7. Rollback if health check failed
    if not health_ok:
        print(f"Health check failed for {username}: {health_err}. Rolling back...")
        if new_c:
            try:
                new_c.stop(timeout=2)
                new_c.remove(force=True)
            except Exception:
                pass
        _rollback(c_name, old_c_temp_name, db_file, bak_file)
        raise HTTPException(status_code=500, detail=f"Update failed health check ({health_err}). Automatically rolled back to previous version.")

    # 8. Success: cleanup backup container & backup sqlite file
    try:
        old_c.remove(force=True)
    except Exception:
        pass

    if bak_file.exists():
        try:
            bak_file.unlink()
        except Exception:
            pass

    # Extract version
    version = "latest"
    try:
        exec_res = new_c.exec_run("node -e \"console.log(require('/app/package.json').version)\"")
        if exec_res.exit_code == 0:
            version = "v" + exec_res.output.decode().strip().lstrip("v")
    except Exception:
        pass

    info["version"] = version
    save_db(db)

    return {
        "success": True,
        "username": username,
        "version": version,
        "message": f"Successfully updated {username} to {version}."
    }

@app.delete("/api/instances/{username}")
def delete_instance(username: str, user=Depends(verify_session)):
    db = load_db()
    if username not in db:
        raise HTTPException(status_code=404, detail="Instance not found")
    c_name = db[username].get("container_name")
    try:
        c = client.containers.get(c_name)
        c.remove(force=True)
    except docker.errors.NotFound:
        pass
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    remove_nginx_config(username)
    del db[username]
    save_db(db)
    return {"success": True}

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>⚡ 9Router Fleet Manager</title>
    <link rel="icon" type="image/svg+xml" href="/favicon.ico">
    <style>
        :root {
            --bg: #0b0f19;
            --card: #151d30;
            --border: #23304d;
            --accent: #38bdf8;
            --accent-hover: #0284c7;
            --text: #f8fafc;
            --text-muted: #94a3b8;
            --success: #10b981;
            --danger: #ef4444;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        body { background: var(--bg); color: var(--text); padding: 24px; }
        .container { max-width: 1200px; margin: 0 auto; }
        header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 16px; }
        h1 { font-size: 24px; font-weight: 700; color: var(--accent); letter-spacing: -0.5px; }
        .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 24px; box-shadow: 0 4px 12px rgba(0,0,0,0.2); }
        .form-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 8px; }
        input, button { padding: 10px 14px; border-radius: 6px; font-size: 14px; border: 1px solid var(--border); background: #0b0f19; color: var(--text); }
        input:focus { outline: none; border-color: var(--accent); }
        button { background: var(--accent); color: #000; font-weight: 600; cursor: pointer; border: none; transition: 0.15s ease-in-out; }
        button:hover { background: var(--accent-hover); }
        button.btn-danger { background: var(--danger); color: #fff; }
        button.btn-danger:hover { background: #dc2626; }
        button.btn-sec { background: var(--border); color: var(--text); }
        button.btn-sec:hover { background: #334155; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; }
        th, td { text-align: left; padding: 12px; border-bottom: 1px solid var(--border); font-size: 14px; vertical-align: top; }
        th { color: var(--text-muted); font-weight: 600; text-transform: uppercase; font-size: 12px; letter-spacing: 0.5px; }
        .badge { display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; text-transform: uppercase; }
        .badge-running { background: rgba(16, 185, 129, 0.2); color: var(--success); }
        .badge-stopped { background: rgba(239, 68, 68, 0.2); color: var(--danger); }
        .actions { display: flex; gap: 6px; flex-wrap: wrap; }
        .actions button { padding: 6px 10px; font-size: 12px; }
        .cred-box { background: #080c14; border: 1px solid #1a253c; padding: 10px; border-radius: 6px; margin-top: 8px; font-family: monospace; font-size: 12px; white-space: pre-wrap; word-break: break-all; }
        @keyframes spin { 100% { transform: rotate(360deg); } }
        .spin { display: inline-block; animation: spin 1s linear infinite; }
        #authOverlay, #editModal { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.85); display: flex; align-items: center; justify-content: center; z-index: 100; }
        .modal { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 24px; width: 380px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }
    </style>
</head>
<body>
    <div id="authOverlay">
        <div class="modal">
            <h2 style="margin-bottom: 12px; color: var(--accent); font-size: 18px;">Fleet Manager Login</h2>
            <input type="text" id="loginUser" placeholder="Admin Username" style="width: 100%; margin-bottom: 12px;">
            <input type="password" id="loginPass" placeholder="Admin Password" style="width: 100%; margin-bottom: 16px;">
            <button onclick="login()" style="width: 100%;">Sign In</button>
        </div>
    </div>

    <div id="editModal" style="display: none;">
        <div class="modal">
            <h2 style="margin-bottom: 12px; color: var(--accent); font-size: 18px;">Edit Instance</h2>
            <label style="font-size: 12px; color: var(--text-muted); display: block; margin-bottom: 4px;">Username:</label>
            <input type="text" id="editUsername" style="width: 100%; margin-bottom: 12px;">
            <label style="font-size: 12px; color: var(--text-muted); display: block; margin-bottom: 4px;">Memo / Department:</label>
            <input type="text" id="editMemo" style="width: 100%; margin-bottom: 12px;">
            <label style="font-size: 12px; color: var(--text-muted); display: block; margin-bottom: 4px;">New Password (Optional):</label>
            <input type="text" id="editNewPass" placeholder="Leave blank to keep unchanged" style="width: 100%; margin-bottom: 16px;">
            <div style="display: flex; gap: 8px;">
                <button onclick="saveEdit()" style="flex: 1;">Save Changes</button>
                <button onclick="closeEdit()" class="btn-sec" style="flex: 1;">Cancel</button>
            </div>
        </div>
    </div>

    <div class="container">
        <header>
            <div>
                <h1>9Router Fleet Manager</h1>
                <p style="color: var(--text-muted); font-size: 13px; margin-top: 4px;">Multi-Tenant 9Router instances with Port Guardian & SSL</p>
            </div>
            <div style="display: flex; align-items: center; gap: 14px;">
                <span id="adminGreeting" style="font-size: 13px; color: var(--accent);"></span>
                <button onclick="logout()" class="btn-sec" style="font-size: 12px;">Sign Out</button>
            </div>
        </header>

        <div class="card">
            <h3 style="margin-bottom: 12px; font-size: 16px;">Create New Instance</h3>
            <div class="form-row">
                <input type="text" id="newUsername" placeholder="Username (e.g. alex)">
                <input type="text" id="newMemo" placeholder="Memo (e.g. Frontend Team)">
                <button onclick="createInstance()">Deploy Instance</button>
            </div>
        </div>

        <div class="card">
            <h3 style="margin-bottom: 12px; font-size: 16px;">Active Instances</h3>
            <table>
                <thead>
                    <tr>
                        <th>User</th>
                        <th>Port</th>
                        <th>Status</th>
                        <th>Version</th>
                        <th>Endpoints & Credentials</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody id="instanceTable">
                    <tr><td colspan="6" style="text-align: center; color: var(--text-muted);">Loading fleet status...</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <script>
        const PROXY_CONFIG = {
            enabled: __ENABLE_UPSTREAM_PROXY__,
            name: "__UPSTREAM_PROXY_NAME__"
        };

        function checkAuth() {
            fetch("/api/auth-check")
                .then(r => {
                    if (r.ok) return r.json();
                    throw new Error("unauth");
                })
                .then(data => {
                    document.getElementById("authOverlay").style.display = "none";
                    document.getElementById("adminGreeting").innerText = "Admin: " + data.user;
                    loadInstances();
                })
                .catch(() => {
                    document.getElementById("authOverlay").style.display = "flex";
                });
        }

        async function login() {
            const username = document.getElementById("loginUser").value.trim();
            const password = document.getElementById("loginPass").value.trim();
            if (!username || !password) return alert("Username and password are required.");
            const res = await fetch("/api/login", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ username, password })
            });
            const data = await res.json();
            if (res.ok) {
                checkAuth();
            } else {
                alert("Error: " + (data.detail || "Authentication failed."));
            }
        }

        async function logout() {
            await fetch("/api/logout", { method: "POST" });
            location.reload();
        }

        async function loadInstances() {
            const res = await fetch("/api/instances");
            if (!res.ok) return;
            const data = await res.json();
            const tbody = document.getElementById("instanceTable");
            tbody.innerHTML = "";
            if (data.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">No instances created yet.</td></tr>';
                return;
            }
            data.forEach(item => {
                const tr = document.createElement("tr");
                const badgeClass = item.status === "running" ? "badge-running" : "badge-stopped";
                const proxyBtn = PROXY_CONFIG.enabled 
                    ? `<button onclick="syncProxy('${item.username}')" class="btn-sec" style="color: var(--accent); border-color: var(--accent);">+ Add Proxy Pool</button>` 
                    : '';
                tr.innerHTML = `
                    <td><strong>${item.username}</strong><br><small style="color: var(--text-muted);">${item.memo || ''}</small></td>
                    <td><code>${item.port}</code></td>
                    <td><span class="badge ${badgeClass}">${item.status}</span></td>
                    <td><code style="color: var(--accent);">${item.version || 'latest'}</code></td>
                    <td>
                        <div class="cred-box">Web Panel: ${item.panel_url}\\nInitial Password: ${item.initial_password}\\nOpenAI Base URL: ${item.base_url}</div>
                    </td>
                    <td class="actions">
                        <button onclick="updateInstance('${item.username}', this)" class="btn-sec" style="color: var(--accent); border-color: var(--accent);">🔄 Update</button>
                        ${proxyBtn}
                        <button onclick="openEdit('${item.username}', '${item.memo || ''}')" class="btn-sec">Edit</button>
                        <button onclick="action('${item.username}', 'restart')" class="btn-sec">Restart</button>
                        <button onclick="del('${item.username}')" class="btn-danger">Delete</button>
                    </td>
                `;
                tbody.appendChild(tr);
            });
        }

        async function updateInstance(username, btn) {
            if (!confirm(`Are you sure you want to update 9Router for ${username} to the latest version?`)) return;
            const origHtml = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<span class="spin">⏳</span> Updating...';
            try {
                const res = await fetch(`/api/instances/${username}/update`, { method: "POST" });
                const data = await res.json();
                if (res.ok) {
                    alert(data.message || `Successfully updated ${username}!`);
                } else {
                    alert("Update Failed: " + (data.detail || JSON.stringify(data)));
                }
            } catch (e) {
                alert("Request error: " + e.message);
            } finally {
                btn.disabled = false;
                btn.innerHTML = origHtml;
                loadInstances();
            }
        }

        async function createInstance() {
            const username = document.getElementById("newUsername").value.trim();
            const memo = document.getElementById("newMemo").value.trim();
            if (!username) return alert("Username is required.");
            const res = await fetch("/api/instances", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ username, memo })
            });
            const data = await res.json();
            if (res.ok) {
                alert("Instance for " + username + " deployed successfully on port " + data.port);
                document.getElementById("newUsername").value = "";
                document.getElementById("newMemo").value = "";
                loadInstances();
            } else {
                alert("Error: " + (data.detail || JSON.stringify(data)));
            }
        }

        let originalUsername = "";
        function openEdit(username, currentMemo) {
            originalUsername = username;
            document.getElementById("editUsername").value = username;
            document.getElementById("editMemo").value = currentMemo;
            document.getElementById("editNewPass").value = "";
            document.getElementById("editModal").style.display = "flex";
        }

        function closeEdit() {
            document.getElementById("editModal").style.display = "none";
        }

        async function saveEdit() {
            const new_username = document.getElementById("editUsername").value.trim();
            const memo = document.getElementById("editMemo").value.trim();
            const new_password = document.getElementById("editNewPass").value.trim();
            const payload = { new_username, memo };
            if (new_password) payload.new_password = new_password;

            const res = await fetch(`/api/instances/${originalUsername}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (res.ok) {
                alert("Instance updated successfully.");
                closeEdit();
                loadInstances();
            } else {
                alert("Error: " + (data.detail || JSON.stringify(data)));
            }
        }

        async function syncProxy(username) {
            if (!confirm(`Inject outbound proxy pool (${PROXY_CONFIG.name}) into ${username}'s SQLite database and restart container?`)) return;
            const res = await fetch(`/api/instances/${username}/sync-proxy`, { method: "POST" });
            const data = await res.json();
            if (res.ok) {
                alert(data.message || `Proxy pool (${PROXY_CONFIG.name}) injected successfully for ${username}.`);
                loadInstances();
            } else {
                alert("Error: " + (data.detail || JSON.stringify(data)));
            }
        }

        async function action(username, act) {
            await fetch(`/api/instances/${username}/action?action=${act}`, { method: "POST" });
            loadInstances();
        }

        async function del(username) {
            if (!confirm("Are you sure you want to delete instance for " + username + "?")) return;
            await fetch(`/api/instances/${username}`, { method: "DELETE" });
            loadInstances();
        }

        checkAuth();
    </script>
</body>
</html>
"""

FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <defs>
    <linearGradient id="grad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#0284c7" />
      <stop offset="100%" stop-color="#38bdf8" />
    </linearGradient>
  </defs>
  <rect width="100" height="100" rx="22" fill="#0b0f19" stroke="#38bdf8" stroke-width="4"/>
  <circle cx="50" cy="50" r="34" fill="none" stroke="#1e293b" stroke-width="6"/>
  <text x="36" y="68" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-size="52" font-weight="900" fill="url(#grad)">9</text>
  <path d="M62 26 L76 26 L76 40 M76 26 L56 46" stroke="#38bdf8" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="76" cy="26" r="4" fill="#38bdf8"/>
</svg>"""

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")

@app.get("/", response_class=HTMLResponse)
def index():
    html = INDEX_HTML.replace("__ENABLE_UPSTREAM_PROXY__", "true" if ENABLE_UPSTREAM_PROXY else "false")
    html = html.replace("__UPSTREAM_PROXY_NAME__", UPSTREAM_PROXY_NAME)
    return html
