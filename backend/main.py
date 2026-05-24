#!/usr/bin/env python3
"""Proximity — Backend API"""

import json, math, hashlib, hmac, base64, os, time, asyncio
from pathlib import Path
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import sqlite3

# ── Config ─────────────────────────────────────────────────────────────────────
SECRET   = os.environ.get("PROX_SECRET", "proximity-dev-secret-2026")
ROOT     = Path(__file__).parent.parent  # repo root
DB_PATH  = Path(os.environ.get("DB_PATH", str(ROOT / "proximity.db")))
FRONTEND = ROOT / "frontend"

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(title="Proximity API", docs_url="/api/docs", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Database ───────────────────────────────────────────────────────────────────
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    con = db()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            email       TEXT UNIQUE NOT NULL,
            pw_hash     TEXT NOT NULL,
            name        TEXT NOT NULL,
            age         INTEGER DEFAULT 25,
            bio         TEXT DEFAULT '',
            emoji       TEXT DEFAULT '😊',
            lat         REAL DEFAULT 0,
            lng         REAL DEFAULT 0,
            last_seen   REAL DEFAULT 0,
            invisible   INTEGER DEFAULT 0,
            mode        TEXT DEFAULT 'dating',
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS likes (
            from_id INTEGER NOT NULL,
            to_id   INTEGER NOT NULL,
            ts      REAL DEFAULT (unixepoch()),
            PRIMARY KEY (from_id, to_id)
        );
        CREATE TABLE IF NOT EXISTS matches (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            u1      INTEGER NOT NULL,
            u2      INTEGER NOT NULL,
            ts      REAL DEFAULT (unixepoch()),
            UNIQUE(u1, u2)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id   INTEGER NOT NULL,
            sender_id  INTEGER NOT NULL,
            body       TEXT NOT NULL,
            ts         REAL DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS waitlist (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            ts    TEXT DEFAULT (datetime('now'))
        );
    """)
    con.commit(); con.close()

# ── Auth helpers ───────────────────────────────────────────────────────────────
def _hash(pw: str) -> str:
    return hashlib.sha256((pw + SECRET).encode()).hexdigest()

def _make_token(user_id: int) -> str:
    payload = base64.b64encode(json.dumps({"id": user_id, "ts": time.time()}).encode()).decode()
    sig = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"

def _verify_token(token: str) -> int:
    try:
        payload, sig = token.split(".")
        expected = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError
        data = json.loads(base64.b64decode(payload))
        return data["id"]
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

def current_user(authorization: str = Header(...)):
    token = authorization.replace("Bearer ", "")
    uid = _verify_token(token)
    con = db()
    row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(status_code=401, detail="User not found")
    return dict(row)

# ── Distance (Haversine, returns feet) ────────────────────────────────────────
def dist_ft(lat1, lng1, lat2, lng2) -> float:
    R = 20902231  # Earth radius in feet
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def fmt_dist(ft: float) -> str:
    if ft < 1000: return f"{int(ft)}ft"
    return f"{ft/5280:.1f}mi"

# ── Pydantic models ────────────────────────────────────────────────────────────
class RegisterReq(BaseModel):
    email: str
    password: str
    name: str
    age: int = 25
    emoji: str = "😊"

class LoginReq(BaseModel):
    email: str
    password: str

class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    age: Optional[int] = None
    bio: Optional[str] = None
    emoji: Optional[str] = None
    mode: Optional[str] = None
    invisible: Optional[bool] = None

class LocationUpdate(BaseModel):
    lat: float
    lng: float

class WaitlistReq(BaseModel):
    email: str

class MessageReq(BaseModel):
    match_id: int
    body: str

# ── WebSocket manager ──────────────────────────────────────────────────────────
class WSManager:
    def __init__(self):
        self.conns: dict[int, WebSocket] = {}

    async def connect(self, uid: int, ws: WebSocket):
        await ws.accept()
        self.conns[uid] = ws

    def disconnect(self, uid: int):
        self.conns.pop(uid, None)

    async def send(self, uid: int, data: dict):
        ws = self.conns.get(uid)
        if ws:
            try: await ws.send_json(data)
            except: self.disconnect(uid)

    async def broadcast(self, uids: list[int], data: dict):
        for uid in uids:
            await self.send(uid, data)

ws_mgr = WSManager()

# Game state: {game_id: {board, turn, players}}
games: dict[str, dict] = {}

# ── Routes ─────────────────────────────────────────────────────────────────────

# Auth
@app.post("/api/register")
def register(req: RegisterReq):
    con = db()
    try:
        con.execute(
            "INSERT INTO users (email, pw_hash, name, age, emoji) VALUES (?,?,?,?,?)",
            (req.email.lower(), _hash(req.password), req.name, req.age, req.emoji)
        )
        con.commit()
        uid = con.execute("SELECT id FROM users WHERE email=?", (req.email.lower(),)).fetchone()["id"]
        return {"token": _make_token(uid), "user_id": uid}
    except sqlite3.IntegrityError:
        raise HTTPException(400, "Email already registered")
    finally:
        con.close()

@app.post("/api/login")
def login(req: LoginReq):
    con = db()
    row = con.execute("SELECT * FROM users WHERE email=? AND pw_hash=?",
                      (req.email.lower(), _hash(req.password))).fetchone()
    con.close()
    if not row:
        raise HTTPException(401, "Wrong email or password")
    return {"token": _make_token(row["id"]), "user_id": row["id"]}

# Profile
@app.get("/api/me")
def get_me(user=Depends(current_user)):
    return user

@app.patch("/api/me")
def update_me(req: ProfileUpdate, user=Depends(current_user)):
    fields = {k: v for k, v in req.dict().items() if v is not None}
    if not fields: return user
    if "invisible" in fields:
        fields["invisible"] = 1 if fields["invisible"] else 0
    sets = ", ".join(f"{k}=?" for k in fields)
    con = db()
    con.execute(f"UPDATE users SET {sets} WHERE id=?", (*fields.values(), user["id"]))
    con.commit()
    row = con.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    con.close()
    return dict(row)

@app.post("/api/location")
def update_location(req: LocationUpdate, user=Depends(current_user)):
    con = db()
    con.execute("UPDATE users SET lat=?, lng=?, last_seen=? WHERE id=?",
                (req.lat, req.lng, time.time(), user["id"]))
    con.commit(); con.close()
    return {"ok": True}

# Nearby users (within ~1 mile, active in last 30 min)
@app.get("/api/nearby")
def nearby(user=Depends(current_user)):
    con = db()
    cutoff = time.time() - 1800  # 30 min
    rows = con.execute(
        "SELECT * FROM users WHERE id!=? AND invisible=0 AND last_seen>?",
        (user["id"], cutoff)
    ).fetchall()
    # Get who current user has already liked
    liked = {r["to_id"] for r in con.execute(
        "SELECT to_id FROM likes WHERE from_id=?", (user["id"],)).fetchall()}
    con.close()

    result = []
    for r in rows:
        ft = dist_ft(user["lat"], user["lng"], r["lat"], r["lng"])
        if ft > 5280:  # 1 mile max
            continue
        result.append({
            "id": r["id"], "name": r["name"], "age": r["age"],
            "bio": r["bio"], "emoji": r["emoji"], "mode": r["mode"],
            "distance": fmt_dist(ft), "distance_ft": ft,
            "liked": r["id"] in liked,
        })
    result.sort(key=lambda x: x["distance_ft"])
    return result

# Likes & matches
@app.post("/api/like/{target_id}")
def like(target_id: int, user=Depends(current_user)):
    con = db()
    try:
        con.execute("INSERT INTO likes (from_id, to_id) VALUES (?,?)", (user["id"], target_id))
        con.commit()
    except sqlite3.IntegrityError:
        con.close()
        return {"matched": False, "already_liked": True}

    # Check mutual
    mutual = con.execute(
        "SELECT 1 FROM likes WHERE from_id=? AND to_id=?", (target_id, user["id"])
    ).fetchone()

    matched = False
    if mutual:
        u1, u2 = sorted([user["id"], target_id])
        try:
            con.execute("INSERT INTO matches (u1, u2) VALUES (?,?)", (u1, u2))
            con.commit()
            matched = True
        except sqlite3.IntegrityError:
            matched = True
    con.close()
    return {"matched": matched}

@app.get("/api/matches")
def get_matches(user=Depends(current_user)):
    uid = user["id"]
    con = db()
    rows = con.execute(
        "SELECT m.id, m.ts, u.id as uid, u.name, u.age, u.emoji, u.bio, u.lat, u.lng "
        "FROM matches m "
        "JOIN users u ON (u.id = CASE WHEN m.u1=? THEN m.u2 ELSE m.u1 END) "
        "WHERE m.u1=? OR m.u2=?",
        (uid, uid, uid)
    ).fetchall()
    result = []
    for r in rows:
        ft = dist_ft(user["lat"], user["lng"], r["lat"], r["lng"])
        result.append({
            "match_id": r["id"], "matched_at": r["ts"],
            "user": {
                "id": r["uid"], "name": r["name"], "age": r["age"],
                "emoji": r["emoji"], "bio": r["bio"],
                "distance": fmt_dist(ft), "distance_ft": ft,
                "lat": r["lat"], "lng": r["lng"],
            }
        })
    con.close()
    return result

# Messages
@app.post("/api/messages")
def send_message(req: MessageReq, user=Depends(current_user)):
    con = db()
    # Verify user is in this match
    match = con.execute(
        "SELECT * FROM matches WHERE id=? AND (u1=? OR u2=?)",
        (req.match_id, user["id"], user["id"])
    ).fetchone()
    if not match:
        raise HTTPException(403, "Not in this match")
    con.execute("INSERT INTO messages (match_id, sender_id, body) VALUES (?,?,?)",
                (req.match_id, user["id"], req.body))
    con.commit(); con.close()
    return {"ok": True}

@app.get("/api/messages/{match_id}")
def get_messages(match_id: int, user=Depends(current_user)):
    con = db()
    match = con.execute(
        "SELECT * FROM matches WHERE id=? AND (u1=? OR u2=?)",
        (match_id, user["id"], user["id"])
    ).fetchone()
    if not match:
        raise HTTPException(403, "Not in this match")
    rows = con.execute(
        "SELECT m.*, u.name, u.emoji FROM messages m JOIN users u ON u.id=m.sender_id "
        "WHERE m.match_id=? ORDER BY m.ts ASC", (match_id,)
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]

# Waitlist
@app.post("/api/waitlist")
def join_waitlist(req: WaitlistReq):
    con = db()
    try:
        con.execute("INSERT INTO waitlist (email) VALUES (?)", (req.email.lower(),))
        con.commit()
        count = con.execute("SELECT COUNT(*) as c FROM waitlist").fetchone()["c"]
        con.close()
        return {"ok": True, "count": count}
    except sqlite3.IntegrityError:
        con.close()
        return {"ok": True, "already": True}

@app.get("/api/waitlist/count")
def waitlist_count():
    con = db()
    c = con.execute("SELECT COUNT(*) as c FROM waitlist").fetchone()["c"]
    con.close()
    return {"count": c + 12847}  # seed number

# ── WebSocket (real-time: location, games, chat) ───────────────────────────────
@app.websocket("/ws/{user_id}")
async def websocket(ws: WebSocket, user_id: int):
    await ws_mgr.connect(user_id, ws)
    try:
        while True:
            data = await ws.receive_json()
            t = data.get("type")

            if t == "ping":
                await ws_mgr.send(user_id, {"type": "pong"})

            elif t == "location":
                # Broadcast location to matched users
                lat, lng = data["lat"], data["lng"]
                con = db()
                con.execute("UPDATE users SET lat=?, lng=?, last_seen=? WHERE id=?",
                            (lat, lng, time.time(), user_id))
                con.commit()
                matches_rows = con.execute(
                    "SELECT CASE WHEN u1=? THEN u2 ELSE u1 END as peer "
                    "FROM matches WHERE u1=? OR u2=?",
                    (user_id, user_id, user_id)
                ).fetchall()
                con.close()
                for row in matches_rows:
                    await ws_mgr.send(row["peer"], {
                        "type": "location_update",
                        "user_id": user_id, "lat": lat, "lng": lng
                    })

            elif t == "game_invite":
                target = data["target_id"]
                game_id = f"{min(user_id,target)}-{max(user_id,target)}"
                games[game_id] = {
                    "board": [""] * 9, "turn": user_id,
                    "players": [user_id, target], "winner": None
                }
                await ws_mgr.send(target, {
                    "type": "game_invite", "from": user_id, "game_id": game_id
                })

            elif t == "game_move":
                game_id = data["game_id"]
                cell = data["cell"]
                g = games.get(game_id)
                if not g or g["turn"] != user_id or g["board"][cell]:
                    continue
                mark = "X" if user_id == g["players"][0] else "O"
                g["board"][cell] = mark
                winner = _check_winner(g["board"])
                g["winner"] = winner
                g["turn"] = [p for p in g["players"] if p != user_id][0]
                await ws_mgr.broadcast(g["players"], {
                    "type": "game_state", "game_id": game_id,
                    "board": g["board"], "turn": g["turn"], "winner": winner
                })

            elif t == "chat":
                match_id = data["match_id"]
                body = data["body"]
                target = data["target_id"]
                con = db()
                con.execute("INSERT INTO messages (match_id, sender_id, body) VALUES (?,?,?)",
                            (match_id, user_id, body))
                con.commit(); con.close()
                await ws_mgr.send(target, {
                    "type": "chat", "match_id": match_id,
                    "from": user_id, "body": body, "ts": time.time()
                })

    except WebSocketDisconnect:
        ws_mgr.disconnect(user_id)

def _check_winner(board):
    wins = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
    for a,b,c in wins:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    if all(board):
        return "draw"
    return None

# ── Serve frontend ─────────────────────────────────────────────────────────────
@app.get("/")
def index(): return FileResponse(FRONTEND / "index.html")

@app.get("/login")
def login_page(): return FileResponse(FRONTEND / "login.html")

@app.get("/signup")
def signup_page(): return FileResponse(FRONTEND / "signup.html")

@app.get("/app")
def app_page(): return FileResponse(FRONTEND / "app.html")

@app.get("/map")
def map_page(): return FileResponse(FRONTEND / "map.html")

@app.get("/game/{game_id}")
def game_page(game_id: str): return FileResponse(FRONTEND / "game.html")

@app.get("/chat/{match_id}")
def chat_page(match_id: int): return FileResponse(FRONTEND / "chat.html")

app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
