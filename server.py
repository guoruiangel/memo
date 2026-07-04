"""
Memo — Pablo Chat v2 (simplified)
- 对话列表 + 任务看板 + 打分 + 归档
- SSO: as=pablo or as=gr
- 稳定优先
"""

import os, sys, json, time, re, threading, sqlite3, subprocess
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template

PORT = 5003
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'pablo_chat.db')
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')

app = Flask(__name__, template_folder=TEMPLATE_DIR)


# ==================== DB ====================

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


def ensure_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL,
            read INTEGER DEFAULT 0,
            reply_to INTEGER DEFAULT 0,
            owner TEXT DEFAULT 'pablo'
        );
        CREATE TABLE IF NOT EXISTS msg_counter (
            id INTEGER PRIMARY KEY CHECK(id=1),
            last_id INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO msg_counter (id,last_id) VALUES (1,0);
        
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id INTEGER,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            score REAL DEFAULT NULL,
            feedback TEXT DEFAULT '',
            created_at REAL NOT NULL,
            completed_at REAL DEFAULT NULL,
            archived INTEGER DEFAULT 0,
            owner TEXT DEFAULT 'pablo',
            FOREIGN KEY(msg_id) REFERENCES messages(id)
        );
        CREATE TABLE IF NOT EXISTS task_counter (
            id INTEGER PRIMARY KEY CHECK(id=1),
            last_id INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO task_counter (id,last_id) VALUES (1,0);
        
        CREATE TABLE IF NOT EXISTS score_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            score REAL NOT NULL,
            reason TEXT DEFAULT '',
            created_at REAL NOT NULL,
            owner TEXT DEFAULT 'pablo',
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
    ''')
    conn.commit()
    conn.close()


# ==================== HELPERS ====================

def store_message(sender, content, now, reply_to=0):
    conn = get_db()
    conn.execute('INSERT INTO messages (sender,content,created_at,read,reply_to) VALUES (?,?,?,0,?)', (sender, content, now, reply_to))
    msg_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.execute('UPDATE msg_counter SET last_id=? WHERE id=1', (msg_id,))
    conn.commit()
    conn.close()
    return msg_id


def store_task(msg_id, title, now):
    conn = get_db()
    conn.execute('INSERT INTO tasks (msg_id,title,status,created_at) VALUES (?,?,?,?)',
                 (msg_id, title, 'pending', now))
    task_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.execute('UPDATE task_counter SET last_id=? WHERE id=1', (task_id,))
    conn.commit()
    conn.close()
    return task_id


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ==================== ROUTES ====================

@app.route('/')
def index():
    return render_template('pablo_chat.html')


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


@app.route('/api/messages')
def api_messages():
    after = request.args.get('after', 0, type=int)
    conn = get_db()
    rows = conn.execute(
        'SELECT id,sender,content,created_at,read,reply_to FROM messages WHERE id>? ORDER BY id ASC', (after,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/tasks', methods=['GET'])
def api_get_tasks():
    archived = request.args.get('archived', 0, type=int)
    conn = get_db()
    rows = conn.execute(
        f'SELECT t.*, m.content as msg_content FROM tasks t LEFT JOIN messages m ON t.msg_id=m.id WHERE t.archived=? ORDER BY t.id DESC',
        (archived,)
    ).fetchall()
    total = conn.execute('SELECT COALESCE(SUM(score),0) as s FROM score_log').fetchone()['s']
    conn.close()
    return jsonify({'tasks': [dict(r) for r in rows], 'total_score': total})


@app.route('/api/tasks', methods=['POST'])
def api_create_task():
    data = request.json
    title = data.get('title', '').strip()
    if not title:
        return jsonify({'error': 'title required'}), 400
    msg_id = data.get('msg_id')
    now = time.time()
    tid = store_task(msg_id, title, now)
    return jsonify({'id': tid, 'status': 'ok'})


@app.route('/api/tasks/<int:task_id>', methods=['PATCH', 'DELETE'])
def api_update_task(task_id):
    data = request.json
    conn = get_db()
    updates = []
    params = []
    for field in ['status', 'feedback', 'archived', 'score']:
        if field in data:
            updates.append(f'{field}=?')
            params.append(data[field])
    if 'completed_at' in data:
        updates.append('completed_at=?')
        params.append(data['completed_at'])
    if request.method == 'DELETE':
        conn.execute('DELETE FROM tasks WHERE id=?', (task_id,))
        conn.execute('DELETE FROM score_log WHERE task_id=?', (task_id,))
        conn.commit()
        conn.close()
        return jsonify({'status': 'ok', 'deleted': True})

    data = request.json
    updates = []
    params = []
    for field in ['status', 'feedback', 'archived', 'score']:
        if field in data:
            updates.append(f'{field}=?')
            params.append(data[field])
    if 'completed_at' in data:
        updates.append('completed_at=?')
        params.append(data['completed_at'])
    if updates:
        params.append(task_id)
        conn.execute(f'UPDATE tasks SET {",".join(updates)} WHERE id=?', params)
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})


@app.route('/api/tasks/<int:task_id>/score', methods=['POST'])
def api_score_task(task_id):
    data = request.json
    score = data.get('score', 0)
    reason = data.get('reason', '')
    now = time.time()

    conn = get_db()
    row = conn.execute('SELECT title FROM tasks WHERE id=?', (task_id,)).fetchone()
    title = row['title'] if row else ''
    conn.execute('UPDATE tasks SET score=?, status=?, feedback=? WHERE id=?',
                 (score, 'scored', reason, task_id))
    conn.execute('INSERT INTO score_log (task_id,score,reason,created_at) VALUES (?,?,?,?)',
                 (task_id, score, reason, now))
    total = conn.execute('SELECT COALESCE(SUM(score),0) as s FROM score_log').fetchone()['s']
    conn.commit()
    conn.close()

    # 同步Dashboard
    try:
        dt = datetime.fromtimestamp(now)
        payload = {
            'year_month': dt.strftime('%Y-%m'),
            'week_number': dt.isocalendar()[1],
            'score_change': int(score),
            'reason': reason or f'#{task_id} {title}',
            'category': '任务加分' if score > 0 else '任务扣分',
            'owner': 'pablo',
            'task_id': task_id,
        }
        threading.Thread(target=lambda: __import__('requests').post(
            'http://127.0.0.1:5000/api/scores', json=payload, timeout=3
        )).start()
    except Exception:
        pass

    return jsonify({'status': 'ok', 'total_score': total})


@app.route('/api/send', methods=['POST'])
def api_send():
    data = request.json
    sender = data.get('sender', 'guorui').strip()
    content = data.get('content', '').strip()
    if not content:
        return jsonify({'error': 'empty'}), 400
    # Memo只允许创建者和Pablo对话
    if sender not in ('guorui', 'pablo'):
        return jsonify({'error': 'forbidden sender'}), 403

    now = time.time()
    reply_to = data.get('reply_to', 0)
    msg_id = store_message(sender, content, now, reply_to)
    log(f"#{msg_id} {sender}: {content[:50]}")

    if sender == 'guorui':
        is_short = len(content) < 15
        is_reply_ref = bool(re.match(r'^#\d+', content.strip()))
        is_ack = content in ['收到', '好的', '可以', 'ok', 'OK', '加油', '很好', '+1'] or content.startswith('不是')
        # 如果消息里引用了已有任务id（如#42），一定是对旧任务的补充
        is_task_ref = bool(re.search(r'#\d+', content))
        if not (is_short or is_reply_ref or is_ack or is_task_ref):
            store_task(msg_id, content[:120], now)
        # 唤醒agent
        threading.Thread(target=_wake_agent, args=(content,), daemon=True).start()

    return jsonify({'id': msg_id, 'status': 'ok'})


def _wake_msg_id():
    """获取当前最新消息id"""
    conn = get_db()
    row = conn.execute('SELECT last_id FROM msg_counter WHERE id=1').fetchone()
    conn.close()
    return row['last_id'] if row else 0


def _agent_cmd(msg, timeout=180):
    env = os.environ.copy()
    env['HOME'] = os.path.expanduser('~')
    env['PATH'] = '/Users/ryan/.nvm/versions/node/v24.15.0/bin:/usr/local/bin:/usr/bin:/bin'
    cmd = [
        '/Users/ryan/.nvm/versions/node/v24.15.0/bin/openclaw', 'agent',
        '--agent', 'main',
        '--message', msg,
        '--json', '--timeout', str(timeout)
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout+30, env=env)


def _schedule_memo_check(minutes):
    """在minutes分钟后创建一个一次性cron任务检查Memo"""
    try:
        after = datetime.utcnow() + timedelta(minutes=minutes)
        at_time = after.strftime('%Y-%m-%dT%H:%M:%SZ')
        msg_id = _wake_msg_id()
        result = subprocess.run([
            '/Users/ryan/.nvm/versions/node/v24.15.0/bin/openclaw', 'cron', 'add', '--json',
            '--body', json.dumps({
                'name': f'memo-check-{int(time.time())}',
                'deleteAfterRun': True,
                'sessionTarget': 'isolated',
                'payload': {
                    'kind': 'agentTurn',
                    'message': f'[Memo检查] 检查Memo(1.11:5003)是否有新消息。如果latest_id>{msg_id}说明有新消息，去处理。如果没有新消息就结束。',
                    'timeoutSeconds': 60
                },
                'delivery': {'mode': 'none'},
                'schedule': {
                    'kind': 'at',
                    'at': at_time
                }
            })
        ], capture_output=True, text=True, timeout=15, env={**os.environ, 'PATH': '/Users/ryan/.nvm/versions/node/v24.15.0/bin:/usr/local/bin:/usr/bin:/bin'})
        log(f"scheduled memo check in {minutes}m: {result.stdout[:100] if result.returncode==0 else result.stderr[:100]}")
    except Exception as e:
        log(f"schedule check err: {e}")


def _wake_agent(content):
    try:
        short = content[:300]
        log(f"waking agent: {short[:40]}...")
        result = _agent_cmd(
            f'[Memo] 用户发了新消息：{short}'
            180
        )
        ok = result.returncode == 0
        log(f"agent: {'ok' if ok else 'fail'}")
        if ok:
            # Agent处理完毕，启动3.7.15检查
            log("3.7.15: scheduling first check in 3min")
            _schedule_memo_check(3)
    except Exception as e:
        log(f"agent err: {e}")


@app.route('/api/latest')
def api_latest():
    limit = request.args.get('limit', 20, type=int)
    conn = get_db()
    msgs = conn.execute(
        'SELECT id,sender,content,created_at,read,reply_to FROM messages ORDER BY id DESC LIMIT ?', (limit,)
    ).fetchall()
    unarchived = conn.execute(
        'SELECT t.*, m.content as msg_content FROM tasks t LEFT JOIN messages m ON t.msg_id=m.id WHERE t.archived=0 ORDER BY t.id DESC'
    ).fetchall()
    archived = conn.execute(
        'SELECT t.*, m.content as msg_content FROM tasks t LEFT JOIN messages m ON t.msg_id=m.id WHERE t.archived=1 ORDER BY t.id DESC LIMIT 50'
    ).fetchall()
    total = conn.execute('SELECT COALESCE(SUM(score),0) as s FROM score_log').fetchone()['s']
    conn.close()
    return jsonify({
        'messages': [dict(r) for r in msgs][::-1],
        'tasks_unarchived': [dict(r) for r in unarchived],
        'tasks_archived': [dict(r) for r in archived],
        'total_score': total
    })


@app.route('/api/mark-read', methods=['POST'])
def api_mark_read():
    conn = get_db()
    c = conn.execute("UPDATE messages SET read=1 WHERE sender='guorui' AND read=0")
    affected = c.rowcount
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok', 'marked': affected})


# ==================== START ====================

if __name__ == '__main__':
    ensure_db()
    log("Memo v2 启动")
    from waitress import serve
    serve(app, host='0.0.0.0', port=PORT, threads=4)
