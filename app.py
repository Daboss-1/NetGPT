import json
import os
import ssl
from urllib import error, request as urllib_request

import certifi
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)


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
        response = urllib_request.urlopen(req, timeout=60, context=context)
    else:
        response = urllib_request.urlopen(req, timeout=60)
    with response as resp:
        return json.loads(resp.read().decode('utf-8'))


@app.route('/api/chat', methods=['POST'])
def chat():
    body = request.get_json(silent=True) or {}
    selected_model = body.get('model', '')
    prompt = body.get('prompt', '')
    messages = body.get('messages', [])
    image_data = body.get('imageData', '')

    if not selected_model or not prompt:
        return jsonify({'error': 'Model and prompt are required.'}), 400

    try:
        if selected_model.startswith('anthropic-'):
            api_key = os.getenv('ANTHROPIC_API_KEY')
            if not api_key:
                return jsonify({'error': 'Missing ANTHROPIC_API_KEY on server.'}), 500

            payload = {
                'model': selected_model.replace('anthropic-', ''),
                'max_tokens': 1024,
                'messages': [
                    msg for msg in messages if msg.get('role') != 'system'
                ] + [{'role': 'user', 'content': prompt}]
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
                'messages': messages + [{'role': 'user', 'content': prompt}]
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
                'messages': messages + [{'role': 'user', 'content': prompt}]
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

        elif selected_model == 'offline':
            offline_model = os.getenv('OFFLINE_MODEL', 'qwen2.5vl:3b')
            offline_base_url = os.getenv('OFFLINE_BASE_URL', 'http://localhost:11434')

            ollama_messages = [
                msg for msg in messages if msg.get('role') != 'system'
            ] + [{'role': 'user', 'content': prompt}]

            if image_data:
                image_base64 = image_data.split(',', 1)[1] if ',' in image_data else image_data
                ollama_messages[-1]['images'] = [image_base64]

            data = post_json(
                f'{offline_base_url.rstrip("/")}/api/chat',
                {
                    'model': offline_model,
                    'messages': ollama_messages,
                    'stream': False
                },
                {}
            )
            assistant_message = (data.get('message') or {
                'role': 'assistant',
                'content': ''
            })

        elif selected_model.startswith('gemini-'):
            api_key = os.getenv('GOOGLE_API_KEY')
            if not api_key:
                return jsonify({'error': 'Missing GOOGLE_API_KEY on server.'}), 500

            contents = [
                {
                    'role': 'model' if msg.get('role') == 'assistant' else 'user',
                    'parts': [{'text': msg.get('content', '')}]
                }
                for msg in messages if msg.get('role') != 'system'
            ] + [{'role': 'user', 'parts': [{'text': prompt}]}]

            data = post_json(
                f'https://generativelanguage.googleapis.com/v1beta/models/{selected_model}:generateContent?key={api_key}',
                {'contents': contents},
                {'Content-Type': 'application/json'}
            )
            assistant_message = {
                'role': 'assistant',
                'content': (((data.get('candidates') or [{}])[0].get('content') or {})
                            .get('parts', [{}])[0].get('text', ''))
            }
        else:
            return jsonify({'error': 'Unsupported model.'}), 400

        return jsonify({'assistantMessage': assistant_message})
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
    app.run(debug=True, host='0.0.0.0', port=80)