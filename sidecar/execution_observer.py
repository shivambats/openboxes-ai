"""Small, content-free execution trace for LangGraph and sidecar capabilities.

Attach ``graph_config`` to a LangGraph invocation inside ``observed_run``.
The callback records node starts and finishes, including nested subgraphs.
``observed_capability`` covers the email paths that do not use LangGraph.
No prompts, email bodies, credentials, or model responses are stored here.
"""
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
import json
import os
import sqlite3
import uuid

from langchain_core.callbacks import BaseCallbackHandler


_ACTIVE = ContextVar('sidecar_execution_observer', default=None)
_PROGRESS_SINK = ContextVar('sidecar_operator_progress_sink', default=None)


@contextmanager
def observe_progress(sink):
    """Publish entered graph/capability nodes to the current chat request."""
    token = _PROGRESS_SINK.set(sink)
    try:
        yield
    finally:
        _PROGRESS_SINK.reset(token)


def progress_step(node, graph_id='operator_chat', message=None):
    """Mark an actual non-graph operation when no traced node wraps it."""
    sink = _PROGRESS_SINK.get()
    if sink:
        try:
            sink(graph_id, node, message)
        except Exception:
            # Progress reporting must not interrupt the operation it describes.
            pass


def _now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


class ExecutionStore:
    def __init__(self, state_path):
        self.path = state_path + '.executions.sqlite3'
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS execution_runs ('
                       'id TEXT PRIMARY KEY, trigger TEXT NOT NULL, graph_id TEXT NOT NULL, '
                       'subject TEXT, status TEXT NOT NULL, started_at TEXT NOT NULL, '
                       'finished_at TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS execution_events ('
                       'sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, '
                       'kind TEXT NOT NULL, graph_id TEXT NOT NULL, node TEXT NOT NULL, '
                       'namespace TEXT NOT NULL, span_id TEXT, parent_span_id TEXT, '
                       'at TEXT NOT NULL, error_type TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS execution_events_run_idx '
                       'ON execution_events(run_id, sequence)')

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute('PRAGMA busy_timeout=10000')
        return db

    def start(self, trigger, graph_id, subject=None):
        run_id = str(uuid.uuid4())
        with self._db() as db:
            db.execute('INSERT INTO execution_runs VALUES (?,?,?,?,?,?,NULL)',
                       (run_id, trigger, graph_id,
                        str(subject)[:160] if subject is not None else None,
                        'running', _now()))
        return run_id

    def event(self, run_id, kind, graph_id, node, namespace=(), *, span_id=None,
              parent_span_id=None, error_type=None):
        with self._db() as db:
            db.execute('INSERT INTO execution_events '
                       '(run_id,kind,graph_id,node,namespace,span_id,parent_span_id,at,error_type) '
                       'VALUES (?,?,?,?,?,?,?,?,?)',
                       (run_id, kind, graph_id, node, json.dumps(list(namespace)),
                        str(span_id) if span_id else None,
                        str(parent_span_id) if parent_span_id else None,
                        _now(), error_type))

    def finish(self, run_id, status):
        with self._db() as db:
            db.execute('UPDATE execution_runs SET status=?, finished_at=? WHERE id=?',
                       (status, _now(), run_id))

    def list_runs(self, limit=30):
        with self._db() as db:
            rows = db.execute('SELECT id,trigger,graph_id,subject,status,started_at,finished_at '
                              'FROM execution_runs ORDER BY started_at DESC LIMIT ?',
                              (min(max(int(limit), 1), 100),)).fetchall()
        return [dict(zip(('id', 'trigger', 'graph_id', 'subject', 'status',
                          'started_at', 'finished_at'), row)) for row in rows]

    def read(self, run_id):
        with self._db() as db:
            run = db.execute('SELECT id,trigger,graph_id,subject,status,started_at,finished_at '
                             'FROM execution_runs WHERE id=?', (run_id,)).fetchone()
            if not run:
                return None
            rows = db.execute('SELECT sequence,kind,graph_id,node,namespace,span_id,'
                              'parent_span_id,at,error_type FROM execution_events '
                              'WHERE run_id=? ORDER BY sequence', (run_id,)).fetchall()
        return {'run': dict(zip(('id', 'trigger', 'graph_id', 'subject', 'status',
                                 'started_at', 'finished_at'), run)),
                'events': [{'sequence': row[0], 'kind': row[1], 'graph_id': row[2],
                            'node': row[3], 'namespace': json.loads(row[4]),
                            'span_id': row[5], 'parent_span_id': row[6],
                            'at': row[7], 'error_type': row[8]} for row in rows]}


class ExecutionObserver:
    def __init__(self, store, run_id, graph_id):
        self.store, self.run_id, self.graph_id = store, run_id, graph_id

    def callback(self, graph_id=None):
        return GraphCallback(self, graph_id or self.graph_id)

    def emit(self, kind, graph_id, node, namespace=(), activity=None, **details):
        # Instrumentation must never stop a shipment workflow.
        try:
            self.store.event(self.run_id, kind, graph_id, node, namespace, **details)
        except (OSError, sqlite3.Error):
            pass
        if kind == 'started':
            try:
                progress_step(node, graph_id, activity)
            except Exception:
                pass

    def capability(self, name, call, graph_id='capabilities', activity=None):
        span_id = str(uuid.uuid4())
        self.emit('started', graph_id, name,
                  (graph_id,), span_id=span_id, activity=activity)
        try:
            result = call()
        except Exception as error:
            self.emit('failed', graph_id, name,
                      (graph_id,), span_id=span_id,
                      error_type=type(error).__name__)
            raise
        self.emit('completed', graph_id, name,
                  (graph_id,), span_id=span_id)
        return result


class GraphCallback(BaseCallbackHandler):
    def __init__(self, observer, graph_id):
        self.observer, self.graph_id = observer, graph_id
        self.spans = {}

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None,
                       tags=None, name=None, **kwargs):
        label = name or (serialized or {}).get('name') or 'chain'
        parent = self.spans.get(str(parent_run_id))
        namespace = parent['namespace'] if parent else (self.graph_id,)
        if label == 'LangGraph':
            if parent and parent['node'] != 'LangGraph':
                namespace = (*namespace, parent['node'])
        else:
            self.observer.emit('started', self.graph_id, label, namespace,
                               span_id=run_id, parent_span_id=parent_run_id)
        self.spans[str(run_id)] = {'node': label, 'namespace': namespace}

    def _finish(self, kind, run_id, parent_run_id=None, error_type=None):
        span = self.spans.pop(str(run_id), None)
        if span and span['node'] != 'LangGraph':
            self.observer.emit(kind, self.graph_id, span['node'], span['namespace'],
                               span_id=run_id, parent_span_id=parent_run_id,
                               error_type=error_type)

    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
        self._finish('completed', run_id, parent_run_id)

    def on_chain_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        self._finish('failed', run_id, parent_run_id, type(error).__name__)


def graph_config(config=None, graph_id=None):
    observer = _ACTIVE.get()
    if observer is None:
        return config
    merged = dict(config or {})
    merged['callbacks'] = [*(merged.get('callbacks') or []), observer.callback(graph_id)]
    return merged


def observed_capability(name, graph_id='capabilities', activity=None):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            observer = _ACTIVE.get()
            if observer is None:
                return fn(*args, **kwargs)
            label = activity(*args, **kwargs) if callable(activity) else activity
            return observer.capability(name, lambda: fn(*args, **kwargs),
                                       graph_id, label)
        return wrapped
    return decorator


def observed_run(graph_id, trigger, subject_arg=None):
    """One decorator marks an entry point; nested capabilities share its run."""
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if _ACTIVE.get() is not None:
                return fn(*args, **kwargs)
            state_path = os.getenv('SIDECAR_STATE_PATH')
            if not state_path:
                return fn(*args, **kwargs)
            subject = kwargs.get(subject_arg) if subject_arg else None
            if subject is None and subject_arg == 'message_id' and args:
                subject = (args[0] or {}).get('message_id') if isinstance(args[0], dict) else None
            observer = _start_trace(state_path, graph_id, trigger, subject)
            if observer is None:
                return fn(*args, **kwargs)
            token = _ACTIVE.set(observer)
            try:
                result = fn(*args, **kwargs)
            except Exception:
                _finish_trace(observer, 'failed')
                raise
            finally:
                _ACTIVE.reset(token)
            _finish_trace(observer, 'completed')
            return result
        return wrapped
    return decorator


@contextmanager
def observe_run(state_path, graph_id, trigger, subject=None):
    """Use when an API already owns the call boundary and state path."""
    if _ACTIVE.get() is not None:
        yield _ACTIVE.get()
        return
    observer = _start_trace(state_path, graph_id, trigger, subject)
    if observer is None:
        yield None
        return
    token = _ACTIVE.set(observer)
    try:
        yield _ACTIVE.get()
    except Exception:
        _finish_trace(observer, 'failed')
        raise
    else:
        _finish_trace(observer, 'completed')
    finally:
        _ACTIVE.reset(token)


def active_observer():
    return _ACTIVE.get()


def observed_service(graph_id, trigger):
    """Decorator for a service method whose ``self.config`` has a state path."""
    def decorator(fn):
        @wraps(fn)
        def wrapped(self, *args, **kwargs):
            with observe_run(self.config.state_path, graph_id, trigger,
                             args[0] if args else None):
                return fn(self, *args, **kwargs)
        return wrapped
    return decorator


def observed_service_stream(graph_id, trigger):
    """Trace each generator step without carrying a context token across yields."""
    def decorator(fn):
        @wraps(fn)
        def wrapped(self, *args, **kwargs):
            def events():
                observer = _start_trace(self.config.state_path, graph_id, trigger,
                                        args[0] if args else None)
                inner = fn(self, *args, **kwargs)
                finished = False
                try:
                    while True:
                        token = _ACTIVE.set(observer) if observer else None
                        try:
                            item = next(inner)
                        except StopIteration:
                            if observer:
                                _finish_trace(observer, 'completed')
                            finished = True
                            return
                        except Exception:
                            if observer:
                                _finish_trace(observer, 'failed')
                            finished = True
                            raise
                        finally:
                            if token:
                                _ACTIVE.reset(token)
                        yield item
                finally:
                    inner.close()
                    if observer and not finished:
                        _finish_trace(observer, 'failed')
            return events()
        return wrapped
    return decorator


def _start_trace(state_path, graph_id, trigger, subject):
    try:
        store = ExecutionStore(state_path)
        run_id = store.start(trigger, graph_id, subject)
    except (OSError, sqlite3.Error):
        return None
    observer = ExecutionObserver(store, run_id, graph_id)
    observer.emit('started', graph_id, 'trigger', (graph_id,))
    return observer


def _finish_trace(observer, status):
    observer.emit('completed' if status == 'completed' else 'failed',
                  observer.graph_id, 'trigger', (observer.graph_id,))
    try:
        observer.store.finish(observer.run_id, status)
    except (OSError, sqlite3.Error):
        pass
