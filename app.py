import base64
import json
import os
import queue
import socket
import sqlite3
import ssl
import struct
import threading
import uuid
from io import BytesIO
from urllib import error, request as urllib_request

import certifi
from flask import Flask, jsonify, render_template, request, session
import fcntl
from pypdf import PdfReader
import time
from werkzeug.security import check_password_hash, generate_password_hash

from docwriter import DocWriter

app = Flask(__name__)
app.secret_key = os.getenv("NETGPT_SECRET", "") or os.urandom(24)

DB_PATH = os.getenv("NETGPT_DB", "netgpt.db")
offline_queue = queue.Queue()
offline_jobs = {}
offline_lock = threading.RLock()
offline_sequence = 0
offline_worker_started = False
ip_poster_started = False


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(chat_id) REFERENCES chats(id)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_chat(user_id: int, title: str | None = None) -> int:
    conn = get_db()
    now = int(time.time())
    try:
        cur = conn.execute(
            """
            INSERT INTO chats (user_id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, title, now, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def touch_chat(chat_id: int) -> None:
    conn = get_db()
    try:
        conn.execute(
            "UPDATE chats SET updated_at = ? WHERE id = ?",
            (int(time.time()), chat_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_chat_title_if_empty(chat_id: int, title: str) -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE chats
            SET title = ?
            WHERE id = ? AND (title IS NULL OR title = '')
            """,
            (title, chat_id),
        )
        conn.commit()
    finally:
        conn.close()


def store_message(chat_id: int, role: str, content: str) -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO chat_messages (chat_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, role, content, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def get_chat_for_user(user_id: int, chat_id: int) -> bool:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM chats WHERE id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()

init_db()


@app.route('/')
def index():
    return render_template('index.html')


def post_json(url, payload, headers):
    data = json.dumps(payload).encode('utf-8')
    request_headers = {
        'Accept': 'application/json',
        'User-Agent': 'NetGPT/1.0',
    }
    request_headers.update(headers)
    req = urllib_request.Request(url, data=data, headers=request_headers, method='POST')
    if url.startswith('https://'):
        context = ssl.create_default_context(cafile=certifi.where())
        response = urllib_request.urlopen(req, timeout=1800, context=context)
    else:
        response = urllib_request.urlopen(req, timeout=1800)
    with response as resp:
        return json.loads(resp.read().decode('utf-8'))


def parse_attachment(attachment):
    if not isinstance(attachment, dict):
        return None

    data_url = attachment.get('dataUrl', '')
    if not data_url:
        return None

    mime_type = attachment.get('type', '') or 'application/octet-stream'
    name = attachment.get('name', '') or 'uploaded file'
    raw_data = data_url.split(',', 1)[1] if ',' in data_url else data_url

    try:
        decoded_bytes = base64.b64decode(raw_data)
    except Exception:
        decoded_bytes = b''

    is_pdf_attachment = mime_type == 'application/pdf' or name.lower().endswith('.pdf')

    if is_pdf_attachment and decoded_bytes:
        try:
            reader = PdfReader(BytesIO(decoded_bytes))
            page_text = []
            for page_number, page in enumerate(reader.pages, start=1):
                extracted_text = page.extract_text() or ''
                page_text.append(f'--- Page {page_number} ---\n{extracted_text.strip()}')

            return {
                'name': name,
                'mime_type': mime_type,
                'kind': 'pdf',
                'page_count': len(reader.pages),
                'content': '\n\n'.join(page_text).strip(),
            }
        except Exception:
            return {
                'name': name,
                'mime_type': mime_type,
                'kind': 'pdf',
                'page_count': 0,
                'content': '',
            }

    is_text_attachment = (
        mime_type.startswith('text/')
        or mime_type in {
            'application/json',
            'application/xml',
            'application/csv',
            'application/javascript',
            'application/x-javascript',
            'application/yaml',
            'application/x-yaml',
        }
        or name.lower().endswith(('.txt', '.md', '.markdown', '.csv', '.json', '.xml', '.yaml', '.yml', '.py', '.js', '.ts', '.html', '.css'))
    )

    if is_text_attachment and decoded_bytes:
        try:
            decoded_text = decoded_bytes.decode('utf-8')
        except Exception:
            decoded_text = decoded_bytes.decode('utf-8', errors='replace')

        return {
            'name': name,
            'mime_type': mime_type,
            'kind': 'text',
            'content': decoded_text,
        }

    if mime_type.startswith('image/'):
        return {
            'name': name,
            'mime_type': mime_type,
            'kind': 'image',
            'content': data_url,
        }

    return {
        'name': name,
        'mime_type': mime_type,
        'kind': 'binary',
        'content': data_url,
    }


def build_attachment_context(attachment):
    if not attachment:
        return ''

    header = [
        f"Uploaded file: {attachment['name']}",
        f"MIME type: {attachment['mime_type']}",
    ]

    if attachment['kind'] == 'text':
        header.append('File contents:')
        header.append(attachment['content'])
        return '\n'.join(header)

    if attachment['kind'] == 'pdf':
        header.append(f"Page count: {attachment.get('page_count', 0)}")
        if attachment['content']:
            header.append('Extracted text:')
            header.append(attachment['content'])
        else:
            header.append('No extractable text was found. This PDF may be scanned or image-only, so OCR would be needed for handwriting or embedded images.')
        return '\n'.join(header)

    if attachment['kind'] == 'image':
        header.append('The image is included separately when the provider supports vision input.')
        return '\n'.join(header)

    header.append('Binary attachment uploaded. The raw data URL is preserved in the request payload.')
    return '\n'.join(header)


def merge_prompt_with_attachment(prompt, attachment_context):
    prompt_text = (prompt or '').strip()
    if prompt_text and attachment_context:
        return f'{prompt_text}\n\n{attachment_context}'
    if attachment_context:
        return attachment_context
    return prompt_text


def extract_base64_data(data_url):
    return data_url.split(',', 1)[1] if ',' in data_url else data_url


def normalize_katex_text(text):
    if not text:
        return text

    segments = text.split('```')
    for index in range(0, len(segments), 2):
        segment = segments[index]
        segment = segment.replace('\\[', '$$').replace('\\]', '$$')
        segment = segment.replace('\\(', '$').replace('\\)', '$')
        segments[index] = segment
    return '```'.join(segments)


def start_offline_worker():
    global offline_worker_started
    if offline_worker_started:
        return

    offline_worker_started = True
    worker = threading.Thread(target=offline_worker_loop, daemon=True)
    worker.start()


def get_interface_ip(interface_name: str) -> str | None:
    if not interface_name:
        return None

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        iface_bytes = interface_name.encode("utf-8")[:15]
        packed = struct.pack("256s", iface_bytes)
        result = fcntl.ioctl(sock.fileno(), 0x8915, packed)
        return socket.inet_ntoa(result[20:24])
    except Exception:
        return None
    finally:
        sock.close()


def start_ip_poster():
    global ip_poster_started
    if ip_poster_started:
        return

    doc_id = os.getenv("GOOGLE_DOC_ID", "1LMWlZho7vclkNMGmsB79mecPxb6iB66hbK9vLgAtMGY").strip()
    if not doc_id:
        return

    ip_poster_started = True
    worker = threading.Thread(target=ip_poster_loop, args=(doc_id,), daemon=True)
    worker.start()


def ip_poster_loop(doc_id: str) -> None:
    interface_name = os.getenv("WIFI_INTERFACE", "wlan0")
    scheme = (os.getenv("NETGPT_PUBLIC_SCHEME", "http") or "http").strip()
    public_port = (os.getenv("NETGPT_PUBLIC_PORT", "") or "").strip()
    port_part = f":{public_port}" if public_port else ""
    writer = DocWriter()
    while True:
        ip_address = get_interface_ip(interface_name)
        if ip_address:
            writer.replace_document_text(doc_id, f"{scheme}://{ip_address}{port_part}\n")
        time.sleep(60)


def offline_worker_loop():
    while True:
        job_id = offline_queue.get()
        try:
            with offline_lock:
                job = offline_jobs.get(job_id)
                if not job:
                    offline_queue.task_done()
                    continue
                job['status'] = 'running'
                job['started_at'] = time.time()

            result = run_offline_request(job['payload'])

            with offline_lock:
                job['status'] = 'done'
                job['result'] = result
                job['finished_at'] = time.time()
            payload = job.get('payload')
            if payload:
                chat_id = payload.get('chat_id')
                if chat_id:
                    store_message(int(chat_id), result.get('role', 'assistant'), result.get('content', ''))
                    touch_chat(int(chat_id))
        except Exception as exc:
            with offline_lock:
                job = offline_jobs.get(job_id)
                if job:
                    job['status'] = 'error'
                    job['error'] = str(exc)
                    job['finished_at'] = time.time()
        finally:
            offline_queue.task_done()


def run_offline_request(payload):
    offline_model = payload.get('offline_model') or os.getenv('OFFLINE_MODEL', 'qwen2.5:1.5b')
    offline_base_url = os.getenv('OFFLINE_BASE_URL', 'http://localhost:11434')

    ollama_messages = payload['messages'] + [{'role': 'user', 'content': payload['prompt']}]
    if payload.get('image_base64'):
        ollama_messages[-1]['images'] = [payload['image_base64']]

    data = post_json(
        f'{offline_base_url.rstrip("/")}/api/chat',
        {
            'model': offline_model,
            'messages': ollama_messages,
            'stream': False
        },
        {}
    )
    message = (data.get('message') or {
        'role': 'assistant',
        'content': ''
    })
    message['content'] = normalize_katex_text(message.get('content', ''))
    return message


def enqueue_offline_job(payload):
    global offline_sequence
    with offline_lock:
        offline_sequence += 1
        job_id = uuid.uuid4().hex
        offline_jobs[job_id] = {
            'id': job_id,
            'status': 'queued',
            'sequence': offline_sequence,
            'created_at': time.time(),
            'payload': payload,
            'result': None,
            'error': None
        }
        offline_queue.put(job_id)
        position = get_offline_position(job_id)
    return job_id, position


def build_ssl_context():
    cert_path = (os.getenv("NETGPT_SSL_CERT", "") or "").strip()
    key_path = (os.getenv("NETGPT_SSL_KEY", "") or "").strip()
    if cert_path and key_path:
        return (cert_path, key_path)
    adhoc = (os.getenv("NETGPT_SSL_ADHOC", "") or "").strip().lower()
    if adhoc in {"1", "true", "yes"}:
        return "adhoc"
    return None


def get_offline_position(job_id):
    with offline_lock:
        job = offline_jobs.get(job_id)
        if not job:
            return 0
        if job['status'] == 'running':
            return 0
        sequence = job['sequence']
        pending = [
            item for item in offline_jobs.values()
            if item['status'] in {'queued', 'running'}
        ]
        return sum(1 for item in pending if item['sequence'] < sequence)


def get_offline_status():
    with offline_lock:
        running = sum(1 for item in offline_jobs.values() if item['status'] == 'running')
        queued = sum(1 for item in offline_jobs.values() if item['status'] == 'queued')
    return {
        'busy': running > 0 or queued > 0,
        'running': running,
        'queued': queued
    }


@app.before_request
def ensure_offline_worker():
    start_offline_worker()
    start_ip_poster()


@app.route('/api/offline/status', methods=['GET'])
def offline_status():
    return jsonify(get_offline_status())


@app.route('/api/offline/job/<job_id>', methods=['GET'])
def offline_job_status(job_id):
    with offline_lock:
        job = offline_jobs.get(job_id)
        if not job:
            return jsonify({'error': 'Unknown job.'}), 404

        response = {
            'status': job['status'],
            'position': get_offline_position(job_id),
            'startedAt': job.get('started_at'),
        }
        if job['status'] == 'done':
            response['assistantMessage'] = job['result']
        if job['status'] == 'error':
            response['error'] = job['error'] or 'Offline request failed.'
        return jsonify(response)


@app.route('/api/auth/signup', methods=['POST'])
def signup():
    body = request.get_json(silent=True) or {}
    email = (body.get('email') or '').strip().lower()
    password = body.get('password') or ''
    if not email or '@' not in email:
        return jsonify({'error': 'Valid email is required.'}), 400
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters.'}), 400

    conn = get_db()
    try:
        password_hash = generate_password_hash(password)
        cur = conn.execute(
            """
            INSERT INTO users (email, password_hash, created_at)
            VALUES (?, ?, ?)
            """,
            (email, password_hash, int(time.time())),
        )
        conn.commit()
        session['user_id'] = cur.lastrowid
        return jsonify({'email': email})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Email is already registered.'}), 409
    finally:
        conn.close()


@app.route('/api/auth/login', methods=['POST'])
def login():
    body = request.get_json(silent=True) or {}
    email = (body.get('email') or '').strip().lower()
    password = body.get('password') or ''
    if not email or not password:
        return jsonify({'error': 'Email and password are required.'}), 400

    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not row or not check_password_hash(row['password_hash'], password):
            return jsonify({'error': 'Invalid email or password.'}), 401
        session['user_id'] = row['id']
        return jsonify({'email': row['email']})
    finally:
        conn.close()


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'status': 'ok'})


@app.route('/api/auth/me', methods=['GET'])
def me():
    user = get_current_user()
    if not user:
        return jsonify({'authenticated': False})
    return jsonify({'authenticated': True, 'email': user['email']})


@app.route('/api/chats', methods=['GET'])
def list_chats():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Login required.'}), 401
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT id, title, updated_at
            FROM chats
            WHERE user_id = ?
            ORDER BY updated_at DESC
            """,
            (user['id'],),
        ).fetchall()
        chats = [
            {
                'id': row['id'],
                'title': row['title'] or 'Untitled chat',
                'updatedAt': row['updated_at'],
            }
            for row in rows
        ]
        return jsonify({'chats': chats})
    finally:
        conn.close()


@app.route('/api/chats', methods=['POST'])
def create_chat_route():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Login required.'}), 401
    body = request.get_json(silent=True) or {}
    title = (body.get('title') or '').strip() or None
    chat_id = create_chat(user['id'], title)
    return jsonify({'chatId': chat_id})


@app.route('/api/chats/<int:chat_id>', methods=['GET'])
def get_chat_messages(chat_id):
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Login required.'}), 401
    if not get_chat_for_user(user['id'], chat_id):
        return jsonify({'error': 'Chat not found.'}), 404
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT role, content
            FROM chat_messages
            WHERE chat_id = ?
            ORDER BY id ASC
            """,
            (chat_id,),
        ).fetchall()
        messages = [{'role': row['role'], 'content': row['content']} for row in rows]
        return jsonify({'messages': messages})
    finally:
        conn.close()


@app.route('/api/chat', methods=['POST'])
def chat():
    body = request.get_json(silent=True) or {}
    selected_model = body.get('model', '')
    prompt = body.get('prompt', '')
    messages = body.get('messages', [])
    chat_id = body.get('chatId')
    attachment = parse_attachment(body.get('attachment'))
    attachment_context = build_attachment_context(attachment)
    prompt_with_attachment = merge_prompt_with_attachment(prompt, attachment_context)

    if not selected_model or (not prompt_with_attachment and not attachment_context):
        return jsonify({'error': 'Model and prompt or attachment are required.'}), 400

    user = get_current_user()
    offline_model = body.get('offlineModel')
    if not offline_model and selected_model.startswith('offline:'):
        offline_model = selected_model.split(':', 1)[1]
    chat_id_int = None
    if user:
        if chat_id:
            try:
                chat_id_int = int(chat_id)
            except ValueError:
                return jsonify({'error': 'Invalid chat id.'}), 400
            if not get_chat_for_user(user['id'], chat_id_int):
                return jsonify({'error': 'Chat not found.'}), 404
        else:
            chat_id_int = create_chat(user['id'])
        if prompt_with_attachment:
            store_message(chat_id_int, 'user', prompt_with_attachment)
            touch_chat(chat_id_int)
            if prompt:
                title_seed = prompt.strip().split('\n', 1)[0][:64]
                if title_seed:
                    set_chat_title_if_empty(chat_id_int, title_seed)

    try:
        if selected_model.startswith('anthropic-'):
            api_key = os.getenv('ANTHROPIC_API_KEY')
            if not api_key:
                return jsonify({'error': 'Missing ANTHROPIC_API_KEY on server.'}), 500

            user_content = []
            if prompt_with_attachment:
                user_content.append({'type': 'text', 'text': prompt_with_attachment})

            if attachment and attachment['kind'] == 'image':
                user_content.append({
                    'type': 'image',
                    'source': {
                        'type': 'base64',
                        'media_type': attachment['mime_type'],
                        'data': extract_base64_data(attachment['content'])
                    }
                })

            payload = {
                'model': selected_model.replace('anthropic-', ''),
                'max_tokens': 1024,
                'messages': [
                    msg for msg in messages if msg.get('role') != 'system'
                ] + [{'role': 'user', 'content': user_content or prompt_with_attachment}]
            }
            data = post_json(
                'https://api.anthropic.com/v1/messages',
                payload,
                {
                    'Content-Type': 'application/json',
                    'x-api-key': api_key,
                    'anthropic-version': '2023-06-01'
                }
            )
            assistant_message = {
                'role': 'assistant',
                'content': (data.get('content') or [{}])[0].get('text', '')
            }

        elif selected_model.startswith('groq-'):
            api_key = os.getenv('GROQ_API_KEY')
            if not api_key:
                return jsonify({'error': 'Missing GROQ_API_KEY on server.'}), 500

            payload = {
                'model': selected_model.replace('groq-', ''),
                'messages': messages + [{'role': 'user', 'content': prompt_with_attachment}]
            }
            data = post_json(
                'https://api.groq.com/openai/v1/chat/completions',
                payload,
                {
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key}'
                }
            )
            assistant_message = (data.get('choices') or [{}])[0].get('message', {
                'role': 'assistant',
                'content': ''
            })

        elif selected_model.startswith('deepseek-'):
            api_key = os.getenv('DEEPSEEK_API_KEY')
            if not api_key:
                return jsonify({'error': 'Missing DEEPSEEK_API_KEY on server.'}), 500

            payload = {
                'model': selected_model,
                'messages': messages + [{'role': 'user', 'content': prompt_with_attachment}]
            }
            data = post_json(
                'https://api.deepseek.com/chat/completions',
                payload,
                {
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {api_key}'
                }
            )
            assistant_message = (data.get('choices') or [{}])[0].get('message', {
                'role': 'assistant',
                'content': ''
            })

        elif selected_model == 'offline' or selected_model.startswith('offline:'):
            messages_without_system = [
                msg for msg in messages if msg.get('role') != 'system'
            ]
            messages_with_system = list(messages_without_system)
            image_base64 = None
            if attachment and attachment['kind'] == 'image':
                image_base64 = extract_base64_data(attachment['content'])

            job_id, position = enqueue_offline_job({
                'messages': messages_with_system,
                'prompt': prompt_with_attachment,
                'image_base64': image_base64,
                'offline_model': offline_model,
                'chat_id': chat_id_int,
            })
            return jsonify({'jobId': job_id, 'position': position, 'chatId': chat_id_int}), 202

        elif selected_model.startswith('gemini-'):
            api_key = os.getenv('GOOGLE_API_KEY')
            if not api_key:
                return jsonify({'error': 'Missing GOOGLE_API_KEY on server.'}), 500

            user_parts = []
            if prompt_with_attachment:
                user_parts.append({'text': prompt_with_attachment})

            if attachment and attachment['kind'] == 'image':
                user_parts.append({
                    'inlineData': {
                        'mimeType': attachment['mime_type'],
                        'data': extract_base64_data(attachment['content'])
                    }
                })

            contents = [
                {
                    'role': 'model' if msg.get('role') == 'assistant' else 'user',
                    'parts': [{'text': msg.get('content', '')}]
                }
                for msg in messages if msg.get('role') != 'system'
            ] + [{'role': 'user', 'parts': user_parts or [{'text': prompt_with_attachment}]}]

            try:
                data = post_json(
                    f'https://generativelanguage.googleapis.com/v1beta/models/{selected_model}:generateContent?key={api_key}',
                    {'contents': contents},
                    {'Content-Type': 'application/json'}
                )
            except error.HTTPError as exc:
                try:
                    provider_error = exc.read().decode('utf-8')
                except Exception:
                    provider_error = str(exc)

                # If Google reports high demand / UNAVAILABLE, retry with exponential backoff
                if exc.code == 503 and ('UNAVAILABLE' in provider_error or 'high demand' in provider_error.lower()):
                    fallback_model = os.getenv('GEMINI_FALLBACK_MODEL', 'gemini-2.5-flash-lite')
                    max_retries = int(os.getenv('GEMINI_RETRIES', '3'))
                    base_delay = float(os.getenv('GEMINI_BASE_DELAY', '1.0'))

                    last_exc = exc
                    for attempt in range(1, max_retries + 1):
                        model_to_try = fallback_model
                        # build an augmented contents payload that includes a short retry note
                        retry_note = {
                            'role': 'user',
                            'parts': [{'text': f"(Automatic retry attempt {attempt} using {model_to_try} due to service high demand.)"}]
                        }
                        attempt_contents = list(contents) + [retry_note]

                        if attempt > 1:
                            delay = base_delay * (2 ** (attempt - 2))
                            time.sleep(delay)

                        try:
                            data = post_json(
                                f'https://generativelanguage.googleapis.com/v1beta/models/{model_to_try}:generateContent?key={api_key}',
                                {'contents': attempt_contents},
                                {'Content-Type': 'application/json'}
                            )
                            last_exc = None
                            break
                        except error.HTTPError as inner_exc:
                            last_exc = inner_exc

                    if last_exc:
                        # All retries failed; re-raise the last exception for outer handler
                        raise last_exc
                else:
                    # Not a transient high-demand error we expect; re-raise
                    raise
            assistant_message = {
                'role': 'assistant',
                'content': (((data.get('candidates') or [{}])[0].get('content') or {})
                            .get('parts', [{}])[0].get('text', ''))
            }
        else:
            return jsonify({'error': 'Unsupported model.'}), 400
        assistant_message['content'] = normalize_katex_text(assistant_message.get('content', ''))
        if chat_id_int:
            store_message(chat_id_int, assistant_message.get('role', 'assistant'), assistant_message.get('content', ''))
            touch_chat(chat_id_int)
        return jsonify({'assistantMessage': assistant_message, 'chatId': chat_id_int})
    except error.HTTPError as exc:
        try:
            provider_error = exc.read().decode('utf-8')
        except Exception:
            provider_error = str(exc)
        if exc.code == 403 and '1010' in provider_error and 'api.groq.com' in provider_error:
            provider_error = (
                'Groq rejected the request with 403/1010. '
                'Verify GROQ_API_KEY is valid and that the account has access to the selected model.'
            )
        return jsonify({'error': provider_error}), exc.code
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500

if __name__ == '__main__':
    start_offline_worker()
    host = os.getenv("NETGPT_HOST", "0.0.0.0")
    port = int(os.getenv("NETGPT_PORT", "80"))
    ssl_context = build_ssl_context()
    app.run(debug=True, host=host, port=port, ssl_context=ssl_context)
