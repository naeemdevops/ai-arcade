"""
AI Game Arcade - Flask backend
Games: AI Trivia Battle (Bedrock writes the questions) and AI Pong (Bedrock coaches you after each match)
Redis: caches AI answers (cache-aside) and stores the live leaderboards
"""
import json
import os
import re
import socket
import time

import redis
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# ---------- Config (environment variables) ----------
AI_MODE = os.getenv("AI_MODE", "bedrock")          # "bedrock" on AWS, "demo" on a laptop without AWS access
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-1")
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "global.amazon.nova-2-lite-v1:0")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))
WORKSHOP_TITLE = os.getenv("WORKSHOP_TITLE", "Cloud Meets AI")
HOST = socket.gethostname()
TIMER_SECONDS = 20
DIFFICULTIES = {"easy", "medium", "hard"}

r = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=3)

bedrock = None
if AI_MODE == "bedrock":
    import boto3
    bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)  # uses the EC2 IAM role, no keys


def ask_ai(prompt, max_tokens=1500):
    """One call to the AI model on Amazon Bedrock (Converse API works for Nova, Claude and others)."""
    resp = bedrock.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0.7},
    )
    # Some models (e.g. Nova 2 with reasoning) return extra blocks; use the first text block
    for block in resp["output"]["message"]["content"]:
        if "text" in block:
            return block["text"]
    raise ValueError("The AI model returned no text")


def cached(key, producer):
    """Cache-aside: return (value, was_cache_hit, milliseconds)."""
    t0 = time.perf_counter()
    hit = r.get(key)
    if hit:
        r.incr("stats:hits")
        value, was_hit = json.loads(hit), True
    else:
        value = producer()
        r.setex(key, CACHE_TTL, json.dumps(value))
        r.incr("stats:misses")
        was_hit = False
    return value, was_hit, round((time.perf_counter() - t0) * 1000)


def clean_name(text):
    return re.sub(r"[^\w .-]", "", str(text))[:20].strip() or "Player"


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "general"


# ---------- Trivia ----------
TRIVIA_PROMPT = """You are a quiz master for a fun trivia game.
Write exactly 5 multiple-choice questions about the topic: "{topic}".
Difficulty: {difficulty}.
Rules: each question has exactly 4 short options, exactly one is correct, facts must be accurate,
explanation is one short sentence.
Return ONLY a JSON array, no other text:
[{{"question": "...", "options": ["A", "B", "C", "D"], "answer": 0, "explanation": "..."}}]
"answer" is the index (0-3) of the correct option."""

DEMO_QUESTIONS = [
    {"question": "Which AWS service gives you Amazon Nova and other AI models through one API?",
     "options": ["Amazon Bedrock", "Amazon S3", "AWS Lambda", "Amazon RDS"], "answer": 0,
     "explanation": "Amazon Bedrock is the managed service for foundation models."},
    {"question": "What does a cache HIT mean?",
     "options": ["Data was found in the cache", "The cache crashed",
                 "Data came from the slow source", "The cache was cleared"], "answer": 0,
     "explanation": "A hit means the answer was served straight from the cache."},
    {"question": "Which Redis data type is ideal for a leaderboard?",
     "options": ["List", "String", "Sorted Set", "Hash"], "answer": 2,
     "explanation": "Sorted sets keep members ordered by score."},
    {"question": "What does an Application Load Balancer do?",
     "options": ["Stores files", "Spreads traffic across servers", "Encrypts disks", "Runs SQL"],
     "answer": 1, "explanation": "An ALB sends each request to a healthy server."},
    {"question": "Which AWS service gives free public SSL/TLS certificates?",
     "options": ["AWS Certificate Manager", "AWS IAM", "Amazon Route 53", "AWS Shield"],
     "answer": 0, "explanation": "ACM issues free certificates for load balancers and CloudFront."},
]


def to_index(ans, opts):
    """Accept 0-3, "2", "C", or the option text itself - different AI models answer differently."""
    if isinstance(ans, bool):
        return None
    if isinstance(ans, int):
        return ans
    if isinstance(ans, str):
        a = ans.strip()
        if a.isdigit():
            return int(a)
        if len(a) == 1 and a.upper() in "ABCD":
            return "ABCD".index(a.upper())
        for i, o in enumerate(opts):
            if str(o).strip().lower() == a.lower():
                return i
    return None


def validate_questions(items):
    out = []
    for q in items:
        if not isinstance(q, dict):
            continue
        opts = q.get("options")
        ans = to_index(q.get("answer"), opts or [])
        if isinstance(q.get("question"), str) and isinstance(opts, list) and len(opts) == 4 \
                and ans is not None and 0 <= ans <= 3:
            out.append({"question": q["question"], "options": [str(o) for o in opts],
                        "answer": ans, "explanation": str(q.get("explanation", ""))})
    if len(out) < 5:
        raise ValueError("AI returned too few valid questions")
    return out[:5]


def make_questions(topic, difficulty):
    if not bedrock:
        time.sleep(1.5)  # pretend to think, so the cache speed-up is visible in demo mode
        return DEMO_QUESTIONS
    text = ask_ai(TRIVIA_PROMPT.format(topic=topic, difficulty=difficulty), 2000)
    return validate_questions(json.loads(text[text.find("["):text.rfind("]") + 1]))


def load_quiz(quiz_id):
    if not isinstance(quiz_id, str) or not quiz_id.startswith("quiz:"):
        return None
    data = r.get(quiz_id)
    return json.loads(data) if data else None


# ---------- Pong coach ----------
COACH_PROMPT = """You are a fun, upbeat esports commentator for a ping-pong video game.
The player just {result} a match against the computer on {difficulty} difficulty.
Final score: player {player}, computer {cpu}. Longest rally: {rally} hits.
Write exactly 2 short sentences: one playful comment on the match, one practical tip to play better.
No emojis, no hashtags, under 45 words in total."""

DEMO_COACH = {
    "won": "What a performance, the computer never saw it coming! Tip: aim for the paddle edges to send sharper angles.",
    "lost": "Tough match, but those rallies showed real promise! Tip: keep your paddle near the centre and react late, not early.",
}


def rally_band(n):
    return "short" if n < 5 else "medium" if n < 12 else "long"


# ---------- Pages ----------
@app.get("/")
def hub():
    return render_template("hub.html", title=WORKSHOP_TITLE)


@app.get("/trivia")
def trivia_page():
    return render_template("trivia.html", title=WORKSHOP_TITLE, timer=TIMER_SECONDS)


@app.get("/pong")
def pong_page():
    return render_template("pong.html", title=WORKSHOP_TITLE)


# ---------- Trivia API ----------
@app.post("/api/questions")
def questions():
    data = request.get_json(silent=True) or {}
    topic = str(data.get("topic", "")).strip()[:60]
    difficulty = data.get("difficulty") if data.get("difficulty") in DIFFICULTIES else "medium"
    if not topic:
        return jsonify(error="Please pick a topic"), 400
    key = f"quiz:{slug(topic)}:{difficulty}"
    try:
        qs, hit, ms = cached(key, lambda: make_questions(topic, difficulty))
    except Exception as e:  # noqa: BLE001
        app.logger.exception("Bedrock call failed")
        return jsonify(error=f"AI could not create questions: {e}"), 502
    public = [{"question": q["question"], "options": q["options"]} for q in qs]  # answers stay on the server
    return jsonify(quiz_id=key, topic=topic, difficulty=difficulty, cached=hit, ms=ms,
                   served_by=HOST, questions=public)


@app.post("/api/answer")
def answer():
    data = request.get_json(silent=True) or {}
    qs = load_quiz(data.get("quiz_id"))
    if not qs:
        return jsonify(error="Quiz expired, please start again"), 410
    try:
        q = qs[int(data.get("index", -1))]
    except (ValueError, IndexError, TypeError):
        return jsonify(error="Bad question index"), 400
    return jsonify(correct=q["answer"], is_correct=data.get("choice") == q["answer"],
                   explanation=q["explanation"])


@app.post("/api/trivia/submit")
def trivia_submit():
    data = request.get_json(silent=True) or {}
    name = clean_name(data.get("name", ""))
    qs = load_quiz(data.get("quiz_id"))
    if not qs:
        return jsonify(error="Quiz expired, please start again"), 410
    correct = score = 0
    for i, a in enumerate((data.get("answers") or [])[:5]):
        if isinstance(a, dict) and a.get("choice") == qs[i]["answer"]:
            correct += 1
            left = max(0, min(int(a.get("time_left", 0) or 0), TIMER_SECONDS))
            score += 100 + left * 5
    r.zadd("lb:trivia", {name: score}, gt=True)
    rank = r.zrevrank("lb:trivia", name)
    return jsonify(name=name, score=score, correct=correct, total=len(qs),
                   rank=rank + 1 if rank is not None else None)


# ---------- Pong API ----------
@app.post("/api/pong/finish")
def pong_finish():
    data = request.get_json(silent=True) or {}
    name = clean_name(data.get("name", ""))
    difficulty = data.get("difficulty") if data.get("difficulty") in DIFFICULTIES else "medium"
    player = max(0, min(int(data.get("player", 0) or 0), 7))
    cpu = max(0, min(int(data.get("cpu", 0) or 0), 7))
    rally = max(0, min(int(data.get("rally", 0) or 0), 200))
    result = "won" if player > cpu else "lost"
    mult = {"easy": 1, "medium": 2, "hard": 3}[difficulty]
    score = (player * 100 + rally * 10 + (300 if result == "won" else 0)) * mult

    # Similar matches share one cached coach comment
    key = f"coach:{result}:{difficulty}:{abs(player - cpu)}:{rally_band(rally)}"

    def produce():
        if not bedrock:
            time.sleep(1.2)
            return DEMO_COACH[result]
        return ask_ai(COACH_PROMPT.format(result=result, difficulty=difficulty, player=player,
                                          cpu=cpu, rally=rally), 200).strip()
    try:
        comment, hit, ms = cached(key, produce)
    except Exception:  # noqa: BLE001
        app.logger.exception("Bedrock call failed")
        comment, hit, ms = "Great game! (The AI coach is taking a break.)", False, 0

    r.zadd("lb:pong", {name: score}, gt=True)
    rank = r.zrevrank("lb:pong", name)
    return jsonify(name=name, score=score, result=result, coach=comment, cached=hit, ms=ms,
                   served_by=HOST, rank=rank + 1 if rank is not None else None)


# ---------- Shared API ----------
@app.get("/api/leaderboard/<game>")
def leaderboard(game):
    if game not in ("trivia", "pong"):
        return jsonify(error="Unknown game"), 404
    top = r.zrevrange(f"lb:{game}", 0, 9, withscores=True)
    return jsonify(players=[{"name": n, "score": int(s)} for n, s in top])


@app.get("/api/stats")
def stats():
    hits, misses = int(r.get("stats:hits") or 0), int(r.get("stats:misses") or 0)
    total = hits + misses
    return jsonify(hits=hits, misses=misses, hit_rate=round(hits * 100 / total) if total else 0,
                   cached_items=sum(1 for _ in r.scan_iter("quiz:*")) + sum(1 for _ in r.scan_iter("coach:*")),
                   served_by=HOST, ai="Bedrock" if bedrock else "Demo")


@app.get("/api/health")
def health():
    """Load balancer health check."""
    try:
        r.ping()
        return jsonify(status="ok", redis="up", host=HOST, ai="bedrock" if bedrock else "demo")
    except redis.RedisError:
        return jsonify(status="degraded", redis="down", host=HOST), 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
