"""
CUP咖啡知识库 — AI问答系统后端
Flask server: 知识检索 + DeepSeek API 代理
"""
import json
import os
import re
from flask import Flask, request, jsonify, send_from_directory
import requests

app = Flask(__name__)

# ── 配置 ──
API_KEY = "sk-47c0d20f71da456aa46f82b3350144fd"
API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-pro"
KB_PATH = os.path.join(os.path.dirname(__file__), "knowledge_base.json")
TOP_K = 5

# ── 加载知识库 ──
with open(KB_PATH, "r", encoding="utf-8") as f:
    KNOWLEDGE_BASE = json.load(f)
print(f"[OK] 知识库已加载，共 {len(KNOWLEDGE_BASE)} 条知识条目")


# ── 知识检索 ──
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


# ── 路由 ──
@app.route("/")
def index():
    return send_from_directory(os.path.dirname(__file__), "index.html")


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True)
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "问题不能为空"}), 400
    if len(question) > 500:
        return jsonify({"error": "问题长度不能超过500字"}), 400

    # 检索知识库
    sources = search_knowledge(question)
    if not sources:
        return jsonify({
            "answer": "抱歉，我目前没有找到与您问题相关的知识库信息，建议您联系门店工作人员咨询。",
            "sources": [],
            "tokens": {"prompt": 0, "completion": 0, "total": 0}
        })

    context = build_context(question, sources)

    # 调用 DeepSeek API
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

    return jsonify({
        "answer": answer,
        "sources": source_list,
        "tokens": tokens
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok", "kb_count": len(KNOWLEDGE_BASE)})


if __name__ == "__main__":
    print("=" * 50)
    print("  CUP咖啡 AI知识库问答系统")
    print("  地址: http://localhost:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=True)
