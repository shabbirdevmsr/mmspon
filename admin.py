import os, secrets
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import quote_plus

import requests
from dotenv import load_dotenv
from flask import Flask, request, redirect, session, flash, render_template_string, url_for
from pymongo import MongoClient
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

def env(k, d=""):
    return os.getenv(k, d)

MONGO_USER = env("MONGO_USER")
MONGO_PASS = env("MONGO_PASS")
MONGO_HOST = env("MONGO_HOST", "localhost")
MONGO_PORT = int(env("MONGO_PORT", "27017"))
MONGO_DB = env("MONGO_DB", "mo8022_bachelor")
TG_API_SERVER = env("TG_API_SERVER", "https://api.telegram.org").rstrip("/")

MONGO_URI = (
    f"mongodb://{quote_plus(MONGO_USER)}:{quote_plus(MONGO_PASS)}"
    f"@{MONGO_HOST}:{MONGO_PORT}/?authSource={quote_plus(MONGO_DB)}"
)
mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000)
db = mongo[MONGO_DB]

bots = db.bots
sources = db.sources
destinations = db.destinations
users = db.users
videos = db.videos
jobs = db.jobs
channel_posts = db.channel_posts
admin_users = db.admin_users
audit = db.audit_logs
admin_users.create_index("username", unique=True)

PANEL_USER = env("PANEL_USER", "admin")
PANEL_PASS = env("PANEL_PASS", "admin")

# First run creates admin/admin. RESET_ADMIN_PASSWORD=1 is useful once after upgrading.
if not admin_users.find_one({"username": PANEL_USER}):
    admin_users.insert_one({
        "username": PANEL_USER,
        "password_hash": generate_password_hash(PANEL_PASS),
        "created_at": datetime.now(timezone.utc),
    })
elif env("RESET_ADMIN_PASSWORD", "1") == "1":
    admin_users.update_one(
        {"username": PANEL_USER},
        {"$set": {"password_hash": generate_password_hash(PANEL_PASS), "updated_at": datetime.now(timezone.utc)}}
    )


def ensure_sources():
    defaults = [
        ("Old API", "legacy", env("OLD_API_URL", "https://shabbir.serv00.net/sex/mmsbaba/get.php")),
        ("VID65 API", "vid65", env("VID65_API_URL", "https://shabbir.serv00.net/sex/vid65/get.php")),
    ]
    for name, typ, url in defaults:
        if not sources.find_one({"base_url": url}):
            sources.insert_one({
                "name": name,
                "type": typ,
                "base_url": url,
                "enabled": True,
                "state": {
                    "current_page": 1,
                    "current_index": 1,
                    "slug": "",
                    "total_pages": 0,
                    "total_items": 0,
                    "has_next": True,
                },
                "created_at": datetime.now(timezone.utc),
            })

ensure_sources()

app = Flask(__name__)
app.secret_key = env("SECRET_KEY", secrets.token_hex(32))

CSS = r'''
:root{--bg:#f4f7fb;--card:#fff;--text:#172033;--muted:#64748b;--line:#e5eaf1;--primary:#4f46e5;--primary2:#4338ca;--green:#059669;--red:#dc2626;--dark:#111827}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Arial,sans-serif}.top{height:64px;background:var(--dark);color:#fff;display:flex;align-items:center;gap:18px;padding:0 24px;position:sticky;top:0;z-index:20}.brand{font-weight:800;margin-right:auto}.top a{color:#cbd5e1;text-decoration:none;font-size:14px}.wrap{max-width:1320px;margin:24px auto;padding:0 18px}.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:20px;margin-bottom:18px;box-shadow:0 8px 28px rgba(15,23,42,.05)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}.stat small,.muted{color:var(--muted)}.stat b{display:block;font-size:28px;margin-top:5px}.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.pill{display:inline-flex;align-items:center;padding:5px 9px;border-radius:999px;background:#eef2ff;color:#4338ca;font-size:12px}.ok{color:var(--green)}.err{color:var(--red)}label{font-size:13px;font-weight:700;color:#475569}input,select{width:100%;padding:12px 13px;border:1px solid #d5dbe5;border-radius:11px;background:#fff;margin:6px 0 12px;outline:none}input:focus,select:focus{border-color:var(--primary);box-shadow:0 0 0 3px #4f46e51a}button,.btn{border:0;border-radius:11px;padding:10px 14px;background:var(--primary);color:#fff;cursor:pointer;text-decoration:none;display:inline-block;font-weight:700}button:hover,.btn:hover{background:var(--primary2)}.red{background:var(--red)}.gray{background:#475569}.green{background:var(--green)}.light{background:#eef2ff;color:#3730a3}.danger-outline{background:#fff;color:var(--red);border:1px solid #fecaca}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px;border-bottom:1px solid #edf0f4;font-size:14px;vertical-align:top}code{background:#f1f5f9;padding:3px 6px;border-radius:7px;word-break:break-all}.hero{display:flex;justify-content:space-between;gap:18px;align-items:center}.hero h1{margin:0 0 6px}.source-card{border:1px solid var(--line);border-radius:15px;padding:16px}.source-head{display:flex;justify-content:space-between;gap:12px;align-items:center}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.empty{text-align:center;padding:45px;color:var(--muted)}.login-shell{min-height:100vh;display:grid;place-items:center;padding:20px}.login-card{width:min(430px,100%)}.login-logo{width:54px;height:54px;border-radius:15px;background:linear-gradient(135deg,#6366f1,#4338ca);display:grid;place-items:center;color:white;font-size:24px;font-weight:900;margin-bottom:15px}.notice{padding:11px 14px;border-radius:10px;background:#ecfdf5;color:#047857;margin-bottom:12px}.notice.err{background:#fef2f2;color:#b91c1c}.bot-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:15px}.bot-choice{display:block;text-decoration:none;color:inherit}.bot-choice .card{height:100%;transition:.15s}.bot-choice .card:hover{transform:translateY(-2px);border-color:#a5b4fc}.bot-avatar{width:48px;height:48px;border-radius:14px;background:#eef2ff;color:#4338ca;display:grid;place-items:center;font-weight:900}.sidebar-title{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:#94a3b8;margin:18px 0 8px}.mini{font-size:12px}.last{font-size:24px;font-weight:800}.mobile-only{display:none}@media(max-width:800px){.top{height:auto;min-height:64px;flex-wrap:wrap;padding:12px 16px}.top a{display:none}.mobile-only{display:inline}.wrap{padding:0 12px}.hero{align-items:flex-start;flex-direction:column}table{display:block;overflow-x:auto;white-space:nowrap}}
'''

LOGIN = r'''<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1"><style>{{css}}</style></head><body><div class=login-shell><div class="card login-card"><div class=login-logo>↯</div><h1>Telegram Manager</h1><p class=muted>Sign in to manage your Telegram bots, APIs and channels.</p>{% if err %}<div class="notice err">{{err}}</div>{% endif %}<form method=post><label>Username</label><input name=username value="{{username or ''}}" placeholder="admin" required><label>Password</label><input name=password type=password placeholder="Password" required><button style="width:100%;margin-top:4px">Sign in</button></form></div></div></body></html>'''

BASE = r'''<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1"><title>{{title}}</title><style>{{css}}</style></head><body><div class=top><div class=brand>Telegram Manager</div>{% if session.get('bot_id') %}<a href="/control">Control</a><a href="/apis">APIs</a><a href="/channels">Channels</a><a href="/jobs">Jobs</a><a href="/users">Users</a><a href="/select-bot">Change bot</a>{% endif %}<a href="/logout">Logout</a></div><div class=wrap>{% with x=get_flashed_messages(with_categories=true) %}{% for c,m in x %}<div class="notice {{'err' if c=='err' else ''}}">{{m}}</div>{% endfor %}{% endwith %}{{body|safe}}</div></body></html>'''


def page(title, body, **ctx):
    return render_template_string(BASE, title=title, body=render_template_string(body, **ctx), css=CSS)


def auth(f):
    @wraps(f)
    def w(*a, **k):
        return f(*a, **k) if session.get("ok") else redirect("/login")
    return w


def selected_bot():
    bid = session.get("bot_id")
    return bots.find_one({"_id": bid}) if bid else None


def tg(token, method, payload=None):
    r = requests.post(f"{TG_API_SERVER}/bot{token}/{method}", json=payload or {}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise ValueError(data.get("description", "Telegram API error"))
    return data


def verify_channel(bot, chat_id):
    me = tg(bot["token"], "getMe").get("result", {})
    member = tg(bot["token"], "getChatMember", {"chat_id": chat_id, "user_id": me.get("id")}).get("result", {})
    return me, member


def channel_last_id(chat_id, bid):
    p = channel_posts.find_one({"bot_id": bid, "chat_id": str(chat_id)}, sort=[("message_id", -1)])
    return p.get("message_id", 0) if p else 0


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("ok"):
        return redirect("/select-bot")
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        u = admin_users.find_one({"username": username})
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session.clear(); session["ok"] = True; session["username"] = u["username"]
            return redirect("/select-bot")
        return render_template_string(LOGIN, css=CSS, err="Invalid username or password", username=username)
    return render_template_string(LOGIN, css=CSS, err="", username="")


@app.get("/logout")
def logout():
    session.clear(); return redirect("/login")


@app.get("/")
@auth
def root():
    return redirect("/select-bot")


@app.get("/select-bot")
@auth
def select_bot():
    bot_list = list(bots.find().sort("created_at", -1))
    body = r'''<div class=hero><div><h1>Select a bot</h1><p class=muted>Choose a bot to open its control panel. Nothing else is shown until a bot is selected.</p></div><a class=btn href=/bots/add>Add new bot</a></div>{% if not bots %}<div class=card><div class=empty><h2>No bots yet</h2><p>Add your first Telegram bot to continue.</p><a class=btn href=/bots/add>Add bot</a></div></div>{% else %}<div class=bot-grid>{% for b in bots %}<a class=bot-choice href="/control?bot_id={{b._id}}"><div class=card><div class=actions><div class=bot-avatar>{{(b.name or 'B')[0]|upper}}</div><div><h2 style="margin:0">{{b.name}}</h2><div class=muted>@{{b.username}}</div></div></div><p><span class=pill>{{'Enabled' if b.enabled else 'Disabled'}}</span> &nbsp; Admin ID: {{b.admin_id}}</p><div class=muted>Click to manage this bot →</div></div></a>{% endfor %}</div>{% endif %}'''
    return page("Select Bot", body, bots=bot_list)


@app.get("/bots/add")
@auth
def add_bot_form():
    body = r'''<div class=hero><div><h1>Add bot</h1><p class=muted>Enter the bot token and your Telegram admin ID.</p></div><a class="btn gray" href=/select-bot>Back</a></div><div class=card><form method=post><div class=row><div><label>Bot token</label><input name=token placeholder="123456:ABC..." required></div><div><label>Telegram admin ID</label><input name=admin_id placeholder="123456789" required></div></div><button>Add bot</button></form></div>'''
    return page("Add Bot", body)


@app.post("/bots/add")
@auth
def addbot():
    try:
        token = request.form["token"].strip()
        r = tg(token, "getMe"); x = r["result"]; bid = str(x["id"])
        bots.update_one({"_id": bid}, {"$set": {
            "token": token, "name": x.get("first_name", "Bot"), "username": x.get("username", ""),
            "admin_id": int(request.form["admin_id"]), "enabled": True, "updated_at": datetime.now(timezone.utc)
        }, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}}, upsert=True)
        session["bot_id"] = bid
        flash("Bot added successfully.", "ok")
        return redirect("/control")
    except Exception as e:
        flash(str(e), "err"); return redirect("/bots/add")


@app.post("/bots/<bid>/delete")
@auth
def delbot(bid):
    bots.delete_one({"_id": bid})
    for c in [destinations, users, videos, jobs, channel_posts]: c.delete_many({"bot_id": bid})
    if session.get("bot_id") == bid: session.pop("bot_id", None)
    flash("Bot removed.", "ok"); return redirect("/select-bot")


@app.get("/control")
@auth
def control():
    bid = request.args.get("bot_id")
    if bid:
        if bots.find_one({"_id": bid}): session["bot_id"] = bid
    b = selected_bot()
    if not b: return redirect("/select-bot")
    ds = list(destinations.find({"bot_id": b["_id"]}).sort("name", 1))
    ss = list(sources.find({"enabled": True}).sort("name", 1))
    bot_jobs = list(jobs.find({"bot_id": b["_id"]}).sort("created_at", -1).limit(10))
    body = r'''<div class=hero><div><h1>{{b.name}}</h1><p class=muted>@{{b.username}} · Bot ID {{b._id}} · Admin ID {{b.admin_id}}</p></div><div class=actions><a class="btn light" href=/select-bot>Change bot</a><form method=post action="/bots/{{b._id}}/delete" onsubmit="return confirm('Delete this bot and its stored data?')"><button class=red>Delete bot</button></form></div></div><div class=grid><div class="card stat"><small>Channels / Groups</small><b>{{ds|length}}</b></div><div class="card stat"><small>Jobs</small><b>{{jobs_count}}</b></div><div class="card stat"><small>Videos</small><b>{{videos_count}}</b></div><div class="card stat"><small>Users</small><b>{{users_count}}</b></div></div><div class=card><div class=hero><div><h2>APIs & current position</h2><p class=muted>Current page, item/index and slug are shown here.</p></div><div class=actions><form method=post action=/run-all><button class=green>Run both APIs</button></form></div></div><div class=grid>{% for s in ss %}<div class=source-card><div class=source-head><b>{{s.name}}</b><span class=pill>{{s.type}}</span></div><p class=mini muted>URL</p><code>{{s.base_url}}</code><div class=grid style="margin-top:12px"><div><div class=mini muted>Page</div><b>{{s.get('state',{}).get('current_page',1)}}</b></div><div><div class=mini muted>Index</div><b>{{s.get('state',{}).get('current_index',1)}}</b></div><div><div class=mini muted>Total</div><b>{{s.get('state',{}).get('total_items',0)}}</b></div><div><div class=mini muted>Pages</div><b>{{s.get('state',{}).get('total_pages',0)}}</b></div></div><p class=mini>Slug: <code>{{s.get('state',{}).get('slug','') or '—'}}</code></p><form method=post action=/run><input type=hidden name=source_id value="{{s._id}}"><button class=green>Run this API</button></form></div>{% endfor %}</div></div><div class=card><div class=hero><div><h2>Channels / Groups</h2><p class=muted>Only chats where this bot is an administrator can be added.</p></div><a class=btn href=/channels>Manage channels</a></div>{% if ds %}<table><tr><th>Name</th><th>Chat</th><th>Bot status</th><th>Last post ID</th><th>Actions</th></tr>{% for d in ds %}<tr><td><b>{{d.name}}</b></td><td>{{d.chat_id}}</td><td class="{{'ok' if d.bot_is_admin else 'err'}}">{{d.bot_status or ('YES' if d.bot_is_admin else 'NO')}}</td><td class=last>{{d.last_post_id or 0}}</td><td><div class=actions><form method=post action="/channels/{{d._id}}/cancel"><button class=gray>Cancel queue</button></form><form method=post action="/channels/{{d._id}}/remove"><button class=red>Remove</button></form></div></td></tr>{% endfor %}</table>{% else %}<div class=empty>No channels added for this bot.</div>{% endif %}</div><div class=card><h2>Recent jobs</h2>{% if jobs %}<table><tr><th>Status</th><th>Sources</th><th>Created</th><th>Error</th></tr>{% for j in jobs %}<tr><td><span class=pill>{{j.status}}</span></td><td>{{j.source_ids|length}}</td><td>{{j.created_at}}</td><td>{{j.get('error','')}}</td></tr>{% endfor %}</table>{% else %}<div class=empty>No jobs yet.</div>{% endif %}</div>'''
    return page("Bot Control", body, b=b, ds=ds, ss=ss, jobs=bot_jobs,
                jobs_count=jobs.count_documents({"bot_id": b["_id"]}),
                videos_count=videos.count_documents({"bot_id": b["_id"]}),
                users_count=users.count_documents({"bot_id": b["_id"]}))


@app.post("/run")
@auth
def run():
    b = selected_bot()
    if not b: return redirect("/select-bot")
    sid = request.form["source_id"]
    jobs.insert_one({"bot_id": b["_id"], "source_ids": [sid], "status": "queued", "created_at": datetime.now(timezone.utc)})
    flash("API job queued.", "ok"); return redirect("/control")


@app.post("/run-all")
@auth
def run_all():
    b = selected_bot()
    if not b: return redirect("/select-bot")
    ids = [str(x["_id"]) for x in sources.find({"enabled": True}, {"_id": 1})]
    jobs.insert_one({"bot_id": b["_id"], "source_ids": ids, "status": "queued", "created_at": datetime.now(timezone.utc)})
    flash("Both APIs queued.", "ok"); return redirect("/control")


@app.get("/apis")
@auth
def apis():
    b = selected_bot()
    if not b: return redirect("/select-bot")
    body = r'''<div class=hero><div><h1>APIs</h1><p class=muted>These two sources are automatically added. Their position is stored in MongoDB.</p></div><a class="btn gray" href=/control>Back to control</a></div><div class=grid>{% for s in sources %}<div class=card><div class=source-head><h2>{{s.name}}</h2><span class=pill>{{s.type}}</span></div><p><code>{{s.base_url}}</code></p><div class=grid><div><small class=muted>Current page</small><div class=last>{{s.get('state',{}).get('current_page',1)}}</div></div><div><small class=muted>Current index</small><div class=last>{{s.get('state',{}).get('current_index',1)}}</div></div><div><small class=muted>Total pages</small><div class=last>{{s.get('state',{}).get('total_pages',0)}}</div></div><div><small class=muted>Total items</small><div class=last>{{s.get('state',{}).get('total_items',0)}}</div></div></div><p>Current slug: <code>{{s.get('state',{}).get('slug','') or '—'}}</code></p></div>{% endfor %}</div>'''
    return page("APIs", body, sources=sources.find({"enabled": True}))


@app.get("/channels")
@auth
def channels_page():
    b = selected_bot()
    if not b: return redirect("/select-bot")
    ds = list(destinations.find({"bot_id": b["_id"]}).sort("name", 1))
    body = r'''<div class=hero><div><h1>Channels / Groups</h1><p class=muted>Bot: @{{b.username}}</p></div><a class="btn gray" href=/control>Back to control</a></div><div class=card><form method=post action=/channels/add><div class=row><div><label>Chat ID or @username</label><input name=chat_id placeholder="@mychannel or -100123..." required></div><div><label>Display name</label><input name=name placeholder="My Channel" required></div></div><button>Add channel</button></form></div><div class=card>{% if ds %}<table><tr><th>Name</th><th>Chat</th><th>Bot admin</th><th>Last post ID</th><th>Actions</th></tr>{% for d in ds %}<tr><td>{{d.name}}</td><td>{{d.chat_id}}</td><td class="{{'ok' if d.bot_is_admin else 'err'}}">{{d.bot_status or ('YES' if d.bot_is_admin else 'NO')}}</td><td class=last>{{d.last_post_id or 0}}</td><td><div class=actions><form method=post action="/channels/{{d._id}}/cancel"><button class=gray>Cancel</button></form><form method=post action="/channels/{{d._id}}/remove"><button class=red>Remove</button></form></div></td></tr>{% endfor %}</table>{% else %}<div class=empty>No channels yet.</div>{% endif %}</div>'''
    return page("Channels", body, b=b, ds=ds)


@app.post("/channels/add")
@auth
def addchan():
    b = selected_bot()
    if not b: return redirect("/select-bot")
    try:
        chat = request.form["chat_id"].strip()
        me, member = verify_channel(b, chat)
        status = member.get("status", "")
        ok = status in ("administrator", "creator")
        if not ok: raise ValueError(f"Bot is not an administrator of this chat (status: {status or 'unknown'})")
        destinations.update_one(
            {"bot_id": b["_id"], "chat_id": chat},
            {"$set": {
                "name": request.form["name"].strip(), "enabled": True, "bot_is_admin": True,
                "bot_status": status, "last_post_id": channel_last_id(chat, b["_id"]),
                "updated_at": datetime.now(timezone.utc)
            }, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}}, upsert=True
        )
        flash("Channel added. Bot is an administrator.", "ok")
    except Exception as e:
        flash(str(e), "err")
    return redirect("/channels")


@app.post("/channels/<cid>/remove")
@auth
def remchan(cid):
    d = destinations.find_one({"_id": cid, "bot_id": session.get("bot_id")})
    destinations.delete_one({"_id": cid, "bot_id": session.get("bot_id")})
    flash("Channel removed.", "ok")
    return redirect("/channels")


@app.post("/channels/<cid>/cancel")
@auth
def cancelchan(cid):
    d = destinations.find_one({"_id": cid, "bot_id": session.get("bot_id")})
    if d:
        jobs.update_many({"bot_id": d["bot_id"], "status": {"$in": ["queued", "claimed", "running"]}}, {"$set": {"status": "cancelled", "cancelled_at": datetime.now(timezone.utc)}})
        flash("Queued/running jobs for this bot were marked cancelled.", "ok")
    return redirect("/control")


@app.get("/jobs")
@auth
def jobs_page():
    b = selected_bot()
    if not b: return redirect("/select-bot")
    body = r'''<div class=hero><div><h1>Jobs</h1><p class=muted>Only jobs for @{{b.username}} are shown.</p></div><a class="btn gray" href=/control>Back</a></div><div class=card><table><tr><th>Status</th><th>Sources</th><th>Created</th><th>Error</th></tr>{% for j in jobs %}<tr><td><span class=pill>{{j.status}}</span></td><td>{{j.source_ids|length}}</td><td>{{j.created_at}}</td><td>{{j.get('error','')}}</td></tr>{% endfor %}</table></div>'''
    return page("Jobs", body, b=b, jobs=jobs.find({"bot_id": b["_id"]}).sort("created_at", -1).limit(200))


@app.get("/users")
@auth
def users_page():
    b = selected_bot()
    if not b: return redirect("/select-bot")
    body = r'''<div class=hero><div><h1>Users</h1><p class=muted>Users registered through @{{b.username}}.</p></div><a class="btn gray" href=/control>Back</a></div><div class=card><table><tr><th>Telegram ID</th><th>Username</th><th>Last seen</th></tr>{% for u in users %}<tr><td>{{u.telegram_id}}</td><td>{{u.get('username','')}}</td><td>{{u.get('last_seen','')}}</td></tr>{% endfor %}</table></div>'''
    return page("Users", body, b=b, users=users.find({"bot_id": b["_id"]}).sort("last_seen", -1).limit(300))


if __name__ == "__main__":
    mongo.admin.command("ping")
    app.run(host=env("PANEL_HOST", "127.0.0.1"), port=int(env("PANEL_PORT", "5000")), debug=False)
