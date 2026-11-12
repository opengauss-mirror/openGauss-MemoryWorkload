"""Hindsight native HTTP adapter. No SDK patches or automatic write retries."""
from datetime import datetime, timezone
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def identifier(value):
    return 'mbp-' + hashlib.sha256(value.encode()).hexdigest()[:32]


def historical_time(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('invalid historical timestamp')
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        parsed = datetime.strptime(value.strip(), '%I:%M %p on %d %B, %Y')
    # LoCoMo supplies no zone. Use a stable UTC convention, not machine-local time.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def positive_int(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError('expected positive integer')
    result = int(value)
    if result <= 0:
        raise ValueError('expected positive integer')
    return result


def native_state(row):
    if not isinstance(row, dict) or row.get('status') not in {
        'pending', 'processing', 'completed', 'failed', 'cancelled'
    }:
        raise ValueError('invalid operation status')
    return row['status']


def run_operation(request, *, environ=None, urlopen=None):
    env = dict(os.environ if environ is None else environ)
    opener = urlopen or urllib.request.build_opener(NoRedirect()).open
    started = time.monotonic()
    operation, output = {}, {}
    try:
        inputs = request.get('inputs') or {}
        scope = inputs.get('scope_id') or request.get('scope_id')
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError('scope_id required')
        bank = identifier(scope)
        operation = {'scope_id': scope, 'bank_id': bank,
                     'runtime_version': env.get('HINDSIGHT_RUNTIME_VERSION', 'unrecorded')}
        base = env.get('HINDSIGHT_API_URL', 'http://127.0.0.1:8888').rstrip('/')
        endpoint = urllib.parse.urlsplit(base)
        if endpoint.scheme not in ('http', 'https') or not endpoint.netloc or endpoint.username or endpoint.query or endpoint.fragment:
            raise ValueError('invalid API URL')
        headers = {'Content-Type': 'application/json'}
        if env.get('HINDSIGHT_API_KEY'):
            headers['Authorization'] = 'Bearer ' + env['HINDSIGHT_API_KEY']
        prefix = '/v1/default/banks/' + bank

        def call(path, body=None, timeout=30):
            req = urllib.request.Request(base + prefix + path, headers=headers,
                data=None if body is None else json.dumps(body).encode(),
                method='GET' if body is None else 'POST')
            with opener(req, timeout=timeout) as response:
                data = json.load(response)
            if not isinstance(data, dict):
                raise ValueError('response must be an object')
            return data

        action = request.get('action')
        state = 'completed'
        if action == 'ingest':
            content = inputs.get('content')
            event = inputs.get('event_id') or request.get('idempotency_key') or request.get('task_id')
            if not isinstance(content, str) or not content.strip() or not event:
                raise ValueError('history and event identity required')
            document = identifier(scope + ':' + str(event))
            operation['session_id'] = document
            item = {'content': content, 'document_id': document,
                    'metadata': {'event_id': str(event)}}
            stamp = inputs.get('occurred_at')
            if stamp is None:
                stamp = inputs.get('timestamp')
            if stamp is not None:
                item['timestamp'] = historical_time(stamp)
            data = call('/memories', {'items': [item], 'async': True})
            task = data.get('operation_id')
            if data.get('success') is not True or data.get('bank_id') != bank or data.get('async') is not True or not isinstance(task, str) or not task:
                raise ValueError('missing asynchronous retain acknowledgement')
            operation['task_id'] = task
            output = {'session_id': document, 'backend_result': data}
            state = 'accepted'
        elif action == 'status':
            previous = inputs.get('operation')
            if not isinstance(previous, dict) or previous.get('bank_id') != bank or previous.get('scope_id') != scope:
                raise ValueError('operation scope mismatch')
            task = previous.get('task_id')
            if not isinstance(task, str) or not task:
                raise ValueError('task_id required')
            operation.update(previous)
            data = call('/operations/' + urllib.parse.quote(task, safe=''))
            if data.get('operation_id') != task:
                raise ValueError('operation identity mismatch')
            status = native_state(data)
            state = 'completed' if status == 'completed' else 'failed' if status in ('failed', 'cancelled') else 'running'
            output = {'backend_result': data}
            if state == 'completed':
                # A private bank makes its full operation queue the readiness boundary:
                # retain success alone can leave observation consolidation running.
                offset, rows = 0, []
                while True:
                    page = call('/operations?' + urllib.parse.urlencode({'limit': 100, 'offset': offset}))
                    batch = page.get('operations')
                    if page.get('bank_id') != bank or not isinstance(batch, list):
                        raise ValueError('missing bank operation list')
                    rows.extend(batch)
                    if len(batch) < 100:
                        break
                    offset += len(batch)
                states = [native_state(row) for row in rows]
                if any(s in ('failed', 'cancelled') for s in states):
                    state = 'failed'
                elif any(s in ('pending', 'processing') for s in states):
                    state = 'running'
                output['bank_operations'] = rows
        elif action == 'recall':
            query = inputs.get('query')
            if not isinstance(query, str) or not query.strip():
                raise ValueError('query required')
            limit = positive_int(inputs.get('node_limit', inputs.get('top_k', 10)))
            if limit > 1000:
                raise ValueError('retrieval limit exceeds 1000')
            budget = env.get('HINDSIGHT_RECALL_BUDGET', 'mid')
            if budget not in ('low', 'mid', 'high'):
                raise ValueError('invalid recall budget')
            tokens = positive_int(env.get('HINDSIGHT_RECALL_MAX_TOKENS', '4096'))
            data = call('/memories/recall', {'query': query, 'budget': budget, 'max_tokens': tokens}, timeout=150)
            rows = data.get('results')
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get('text'), str) or not row['text'].strip() for row in rows):
                raise ValueError('invalid recall results')
            # Keep native ranking; Workload's node_limit caps the supplied evidence.
            selected = rows[:limit]
            output = {'backend_result': data, 'memories': selected, 'node_limit': limit,
                      'budget': budget, 'max_tokens': tokens,
                      'evidence_text': '\n\n'.join(row['text'] for row in selected)}
        else:
            raise ValueError('unsupported action')
        result = {'status': 'failed' if state == 'failed' else 'ok', 'state': state,
                  'operation': operation, 'output': output,
                  'error': {'code': 'backend_task_failed', 'message': 'Hindsight background operation failed'} if state == 'failed' else {}}
    except Exception as exc:
        # Never include credential-bearing URLs, remote bodies or prompts in errors.
        result = {'status': 'failed', 'state': 'failed', 'operation': operation, 'output': output,
                  'error': {'code': type(exc).__name__, 'message': 'Hindsight operation failed: ' + type(exc).__name__}}
    result.update(metrics=[{'name': 'memory_runner_ms', 'value': (time.monotonic()-started)*1000, 'unit': 'ms'}], artifacts=[])
    return result


if __name__ == '__main__':
    json.dump(run_operation(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
