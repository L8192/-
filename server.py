"""
CUP咖啡知识库 — AI问答系统后端
Flask + SQLite：用户认证 + 对话记录
"""
import json
import os
import re
import hashlib
import secrets
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, g
import requests

app = Flask(__name__)

# ── 配置 ──
API_KEY = os.environ.get("API_KEY", "")
# 本地开发：尝试从 .env 文件读取
if not API_KEY:
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("API_KEY="):
                    API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
if not API_KEY:
    raise RuntimeError("请设置 API_KEY 环境变量或创建 .env 文件，参见 .env.example")
API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-pro"
BASE_DIR = os.path.dirname(__file__)
KB_PATH = os.path.join(BASE_DIR, "knowledge_base.json")
DB_PATH = os.path.join(BASE_DIR, "db.sqlite3")
TOP_K = 5

# ── 加载知识库 ──
with open(KB_PATH, "r", encoding="utf-8") as f:
    KNOWLEDGE_BASE = json.load(f)
print(f"[OK] 知识库已加载，共 {len(KNOWLEDGE_BASE)} 条")


# ═══════════════════════════════════════
#  数据库
# ═══════════════════════════════════════

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                username   TEXT UNIQUE NOT NULL,
                password   TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                question   TEXT NOT NULL,
                answer     TEXT NOT NULL,
                sources    TEXT,
                tokens     TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        conn.commit()
    print("[OK] 数据库初始化完成")


init_db()


# ── 工具函数 ──

def hash_password(password: str) -> str:
    salt = "CUPcoffee2026"
    return hashlib.sha256((password + salt).encode()).hexdigest()


def get_user_by_token(token: str):
    """根据 token 获取用户，失败返回 None"""
    if not token:
        return None
    db = get_db()
    row = db.execute(
        "SELECT u.id, u.username FROM users u "
        "JOIN sessions s ON u.id = s.user_id "
        "WHERE s.token = ?", (token,)
    ).fetchone()
    return dict(row) if row else None


def require_auth():
    """从请求头获取 token 并返回用户，未登录返回 None"""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    return get_user_by_token(token)


# ═══════════════════════════════════════
#  知识检索
# ═══════════════════════════════════════

def search_knowledge(question: str, top_k: int = TOP_K):
    tokens = set()
    for size in [4, 3, 2]:
        for i in range(len(question) - size + 1):
            seg = question[i:i + size]
            if re.search(r'[一-鿿]', seg):
                tokens.add(seg)
    for w in re.findall(r'[a-zA-Z]+', question.lower()):
        if len(w) >= 2:
            tokens.add(w)
    for n in re.findall(r'\d+', question):
        tokens.add(n)

    if not tokens:
        return []

    scored = []
    for item in KNOWLEDGE_BASE:
        text = (item.get("title", "") + " " + item.get("content", "")).lower()
        score = sum(1 for t in tokens if t.lower() in text)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:top_k]]


def build_context(question: str, sources: list) -> str:
    parts = []
    for i, s in enumerate(sources, 1):
        parts.append(
            f"[知识{i}] [{s.get('category', '')}] {s.get('title', '')}\n{s.get('content', '')}"
        )
    return "\n\n".join(parts)


def build_system_prompt() -> str:
    return (
        "你是CUP咖啡大学城旗舰店的AI助手，名字叫「小C」。\n"
        "请严格遵循以下规则：\n"
        "1. 仅基于「知识库参考」中的内容回答用户问题。\n"
        "2. 如果知识库中没有相关信息，请如实告知「抱歉，我目前没有这方面的信息，建议您联系门店工作人员咨询。」\n"
        "3. 回答简洁友好，使用中文，语气温暖但不啰嗦。\n"
        "4. 如果涉及价格，请明确标注单位和杯型（中杯/大杯）。\n"
        "5. 如果用户问的是2025年或更早的信息，请提示该信息可能已过期，并尽量提供2026年现行标准。\n"
        "6. 不要编造知识库中没有的信息。"
    )


# ═══════════════════════════════════════
#  页面
# ═══════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


# ═══════════════════════════════════════
#  认证
# ═══════════════════════════════════════

@app.route("/register", methods=["POST"])
def register():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    if len(username) < 2 or len(username) > 20:
        return jsonify({"error": "用户名需2-20个字符"}), 400
    if len(password) < 4:
        return jsonify({"error": "密码至少4位"}), 400
    if not re.match(r'^[a-zA-Z0-9_一-鿿]+$', username):
        return jsonify({"error": "用户名只能包含中英文、数字和下划线"}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        return jsonify({"error": "用户名已被注册"}), 409

    db.execute("INSERT INTO users (username, password) VALUES (?, ?)",
               (username, hash_password(password)))
    db.commit()

    # 注册后自动登录
    user = db.execute("SELECT id, username FROM users WHERE username = ?", (username,)).fetchone()
    token = secrets.token_hex(32)
    db.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user["id"]))
    db.commit()

    return jsonify({
        "ok": True,
        "token": token,
        "user": {"id": user["id"], "username": user["username"]}
    })


@app.route("/login", methods=["POST"])
def login():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400

    db = get_db()
    user = db.execute("SELECT id, username, password FROM users WHERE username = ?",
                      (username,)).fetchone()
    if not user or user["password"] != hash_password(password):
        return jsonify({"error": "用户名或密码错误"}), 401

    token = secrets.token_hex(32)
    db.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user["id"]))
    db.commit()

    return jsonify({
        "ok": True,
        "token": token,
        "user": {"id": user["id"], "username": user["username"]}
    })


@app.route("/logout", methods=["POST"])
def logout():
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token:
        db = get_db()
        db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        db.commit()
    return jsonify({"ok": True})


@app.route("/me", methods=["GET"])
def me():
    user = require_auth()
    if not user:
        return jsonify({"user": None})
    return jsonify({"user": user})


# ═══════════════════════════════════════
#  对话（核心）
# ═══════════════════════════════════════

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True)
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "问题不能为空"}), 400
    if len(question) > 500:
        return jsonify({"error": "问题长度不能超过500字"}), 400

    # 1. 检索
    sources = search_knowledge(question)
    if not sources:
        answer = "抱歉，我目前没有找到与您问题相关的知识库信息，建议您联系门店工作人员咨询。"
        tokens = {"prompt": 0, "completion": 0, "total": 0}
        source_list = []
    else:
        context = build_context(question, sources)
        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": build_system_prompt()},
                {"role": "user", "content": f"知识库参考：\n\n{context}\n\n用户问题：{question}\n\n请基于以上知识库参考回答。"}
            ],
            "temperature": 0.3,
            "max_tokens": 800,
        }
        try:
            resp = requests.post(API_URL, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            result = resp.json()
            answer = result["choices"][0]["message"]["content"]
            usage = result.get("usage", {})
            tokens = {
                "prompt": usage.get("prompt_tokens", 0),
                "completion": usage.get("completion_tokens", 0),
                "total": usage.get("total_tokens", 0)
            }
        except requests.exceptions.Timeout:
            return jsonify({"error": "AI服务响应超时，请稍后重试"}), 504
        except requests.exceptions.RequestException as e:
            return jsonify({"error": f"AI服务请求失败：{str(e)}"}), 502
        except (KeyError, IndexError):
            return jsonify({"error": "AI服务返回格式异常"}), 502

        source_list = [
            {"id": s["id"], "title": s["title"], "category": s["category"]}
            for s in sources
        ]

    # 2. 保存对话记录（仅登录用户）
    user = require_auth()
    if user:
        db = get_db()
        db.execute(
            "INSERT INTO chat_history (user_id, question, answer, sources, tokens) VALUES (?, ?, ?, ?, ?)",
            (user["id"], question, answer,
             json.dumps(source_list, ensure_ascii=False),
             json.dumps(tokens, ensure_ascii=False))
        )
        db.commit()

    return jsonify({
        "answer": answer,
        "sources": source_list,
        "tokens": tokens
    })


# ═══════════════════════════════════════
#  对话历史
# ═══════════════════════════════════════

@app.route("/history", methods=["GET"])
def history():
    user = require_auth()
    if not user:
        return jsonify({"error": "请先登录"}), 401

    db = get_db()
    rows = db.execute(
        "SELECT id, question, answer, sources, tokens, created_at "
        "FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT 50",
        (user["id"],)
    ).fetchall()

    items = []
    for r in rows:
        items.append({
            "id": r["id"],
            "question": r["question"],
            "answer": r["answer"],
            "sources": json.loads(r["sources"]) if r["sources"] else [],
            "tokens": json.loads(r["tokens"]) if r["tokens"] else {},
            "created_at": r["created_at"]
        })

    return jsonify({"history": items})


@app.route("/history/clear", methods=["POST"])
def clear_history():
    user = require_auth()
    if not user:
        return jsonify({"error": "请先登录"}), 401

    db = get_db()
    db.execute("DELETE FROM chat_history WHERE user_id = ?", (user["id"],))
    db.commit()
    return jsonify({"ok": True})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "kb_count": len(KNOWLEDGE_BASE)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") != "production"
    print("=" * 50)
    print("  CUP咖啡 AI知识库问答系统")
    print(f"  地址: http://localhost:{port}")
    print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=debug)
