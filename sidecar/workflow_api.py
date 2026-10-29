"""Local API and UI for the checkpointed incident workflow.

Run from sidecar/: .venv/bin/uvicorn workflow_api:app --host 0.0.0.0 --port 8091
The API reads OpenBoxes, calls OpenRouter, and can send test-only recovery
availability inquiries for synthetic shipments. It does not write WMS data.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from action_ledger import (ActionLedger, current_instant, instant_text, parse_instant,
                           readable_instant)
from case_graph import private_database_file, sqlite_checkpointer
from case_graph import ShipmentCaseGraph
from carrier_dispatch_reply import (extract_carrier_vehicle_report,
                                    selected_carrier_replies)
from config import Config
from contact_follow_up import ContactFollowUpMonitor, ShipmentActivityRecorder
from conditional_recovery_replies import (recovery_review_graph,
                                          recovery_review_topology,
                                          _complete_candidate)
from delay_outreach import JevRouter, OutreachJournal, select_contact
from email_orchestrator import AgentMail, OpenRouter, ShipmentEmailGraph, load_local_env
from entity_resolution import extract_shipment_entity
from execution_observer import (ExecutionStore, active_observer, graph_config,
                                observed_capability, observed_service,
                                observed_service_stream, observe_progress, observe_run,
                                progress_step)
from fleet_config import CONDITIONAL_QUOTE_WINDOW_MINUTES, SHIPPERS
from fleet_manager_agent import FleetOutreachJournal
from incident_recovery_outreach import coordinate_incident_outreach
from incident_workflow import IncidentWorkflow
from image_graph import IMAGE_TYPES, MAX_IMAGE_BYTES, ShipmentImageGraph
from operator_chat import (OperatorChatGraph, OperatorChatStore, conversation_evidence,
                           conversation_for_model, operator_chat_topology, progress_hub)
from operator_attention import attention_facts, recall_alerts_facts
from operator_shipment_search import delayed_shipments_view, shipment_card_actions
from operator_case_state import (case_for_model, case_projection, carrier_cards,
                                 carrier_vehicle_dispatched,
                                 load_transfer_facts, load_transfer_sources,
                                 load_transfer_status, status_view)
from operator_quote_actions import (configured_carriers, interpret_quote_request,
                                    quote_facts, send_vendor_followup)
from operator_dispatch import (request_carrier_confirmation, notify_driver,
                               operator_dispatch_topology)
from operator_driver_check import CONTACT_SEND, model_contact_drafter, send_contact_check
from chat_insights import findings, insights_card
from chat_links import card_labels, link_cards, link_reply
from next_steps import JevNextStepRanker, suggest
from shipment_investigation import (email_sources, incident_chat_sources, investigate,
                                    openboxes_sources)
from chat_components import JevComponentChooser, block, render as render_component
from data_agent import answer_question
from operator_contact_replies import (ContactReplyStore, check_contact_replies,
                                      contact_reply_facts, read_contact_thread,
                                      verified_contact_messages)
from operator_capabilities import (CAPABILITY_GRAPH, MAX_EFFECTS, MAX_STEPS, Actor, Capability,
                                   CapabilityRegistry, JevCapabilitySelector, OperatorLoop,
                                   compose, follow_up_window, sla_status)
from operator_focused_answer import focused_reply
from operator_public_reply import private_record_ids, public_reply_text
from operator_recall import RecallChatNoticeStore, fresh_recall_check, recall_facts
from operator_tow_search import (BrowserTowPages, TowLeadStore, check_quote_replies,
                                 google_tow_leads, send_quote_inquiries,
                                 tow_cards)
from recovery_dispatch import prepare_dispatch_request, send_dispatch_request
from recovery_trip import create_recovery_shipment as create_recovery_trip
from backup_truck import driver_request, outcome as backup_truck_outcome
from conditional_recovery_replies import _complete_candidate
from eta_verification import (JevEtaAssessor, check_facts, effective_quotes, place_for,
                              question_key, route, settle, truck_start, write_eta_question)
from recovery_quotes import select_quote
from inspection_flow import (FOLLOW_UP_LIMIT, JevInspectionAssessor, case_context,
                             extract_inspection_report, inspector_replies, last_activity,
                             next_inspection_step, reply_on_thread, silence_due, step_key,
                             write_booking, write_inspection_follow_up,
                             write_inspection_nudge)
from broker_next_step import (JevBrokerReplyAssessor, decision_context, send_broker_follow_up,
                              settled)
from tools.openboxes import OpenBoxesTool, OpsTaskRejected
from warehouse_ops import (SupplierJournal, diagnose_requests, find_order_reference,
                           propose_purchase_orders, read_supplier_reply, review_bins, review_expiry,
                           review_purchase_orders, review_receiving, supplier_email, table,
                           task_key)


HERE = Path(__file__).resolve().parent
QUOTE_MONITOR_INTERVAL_SECONDS = 60
SAMPLE_SHIPMENT = {
    'id': 'sample-incident-1', 'shipmentNumber': 'SAMPLE-ROAD-1',
    'origin': 'Ohio Warehouse', 'destination': 'California Distribution Center',
    'status': 'DELAYED', 'version': 1, 'synthetic': True,
    'items': [{'quantity': 20, 'product_name': 'Sample cartons'}],
    'late_delivery_fee_usd': 10000,
}


def utcnow():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def progress_ticket(secret, request_id, user_id, location_id, ttl_seconds=180):
    """Issue a short-lived capability for one user's progress socket."""
    payload = {'request_id': request_id, 'user_id': user_id,
               'location_id': location_id, 'expires': int(time.time()) + ttl_seconds}
    encoded = base64.urlsafe_b64encode(json.dumps(
        payload, separators=(',', ':'), sort_keys=True).encode()).decode().rstrip('=')
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + '.' + signature


def verify_progress_ticket(secret, ticket, request_id):
    if not secret or not ticket or len(ticket) > 1200:
        return None
    try:
        encoded, signature = ticket.split('.', 1)
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        raw = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
        payload = json.loads(raw)
        if payload.get('request_id') != request_id or \
                not isinstance(payload.get('user_id'), str) or \
                not isinstance(payload.get('location_id'), str) or \
                not isinstance(payload.get('expires'), int) or \
                payload['expires'] < int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, KeyError, binascii.Error, UnicodeDecodeError):
        return None


def local_config():
    """Read sidecar credentials without changing process-wide test/runtime env."""
    original = dict(os.environ)
    try:
        load_local_env()
        credential_file = os.getenv('SHIPMENT_EMAIL_ENV_FILE')
        if credential_file:
            load_local_env(credential_file, overwrite=True)
        return Config()
    finally:
        os.environ.clear()
        os.environ.update(original)


class NewSession(BaseModel):
    shipment_id: str = Field(min_length=1, max_length=100)


class ChatMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    image_data_url: str | None = Field(default=None, max_length=12_000_000)


class OperatorMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    shipment_context: dict | None = None
    recall_context: dict | None = None
    attention_context: dict | None = None
    shipment_search_context: dict | None = None
    excluded_shipment_reference: str | None = None
    intent_type: str | None = None
    entity_source_message_id: str | None = Field(default=None, max_length=100)
    contact_role: str | None = None
    tow_service_reference: str | None = None
    resolution_note: str | None = None
    request_id: str | None = None
    # Signed by OpenBoxes for the asking user's warehouse; carried, never read or made.
    data_ticket: str | None = Field(default=None, max_length=1000)


class ConsoleOperatorMessage(BaseModel):
    console_id: str
    text: str = Field(min_length=1, max_length=4000)
    request_id: str | None = None
    # Lets the console exercise the read-only user's permission paths.
    access: str = Field(default='write', pattern='^(read|write)$')


class OperatorFocus(BaseModel):
    shipment_number: str = Field(min_length=1, max_length=100)
    shipment_id: str | None = Field(default=None, max_length=100)


class ConsoleOperatorFocus(BaseModel):
    console_id: str
    shipment_id: str | None = Field(default=None, max_length=100)
    shipment_number: str | None = Field(default=None, max_length=100)


class ChatProgressStart(BaseModel):
    request_id: str


class ChatProgressStage(ChatProgressStart):
    stage: str
    sources: list[str] = Field(default_factory=list)
    shipment_reference: str | None = Field(default=None, max_length=255)


class DispatchApproval(BaseModel):
    quote_id: str = Field(min_length=1, max_length=100)
    # Taken from the inspector's recorded report when there is one.
    inspection_reference: str | None = Field(default=None, min_length=3, max_length=100)
    cargo_fit_for_transfer: bool | None = None
    scene_cleared: bool
    safe_pickup_point: str = Field(min_length=3, max_length=300)
    vehicle_ids: str = Field(min_length=3, max_length=150)
    approved_price_usd: str = Field(min_length=1, max_length=30)
    fleet_reviewer: str = Field(min_length=3, max_length=100)


class UnknownSessionError(LookupError):
    pass


def decode_image(data_url):
    if not data_url:
        return None
    header, separator, encoded = data_url.partition(',')
    if not separator or not header.startswith('data:') or not header.endswith(';base64'):
        raise ValueError('A base64 image data URL is required')
    mime_type = header[5:-7].lower()
    if mime_type not in IMAGE_TYPES:
        raise ValueError('Unsupported image type')
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('Invalid image encoding') from None
    if not 0 < len(image) <= MAX_IMAGE_BYTES:
        raise ValueError('Image must be 8 MB or smaller')
    return image, mime_type


def image_topology():
    drawable = ShipmentImageGraph(None).graph.get_graph()
    return {'id': 'image_analysis', 'title': 'Image analysis subgraph',
            'nodes': [{'id': key, 'label': key.replace('_', ' ').title()}
                      for key in drawable.nodes
                      if key not in {'__start__', '__end__'}],
            'edges': [{'source': edge.source, 'target': edge.target}
                      for edge in drawable.edges
                      if edge.source not in {'__start__', '__end__'} and
                      edge.target not in {'__start__', '__end__'}]}


def shipment_resolution_topology():
    return {'id': 'shipment_resolution', 'title': 'Shipment identification',
            'nodes': [{'id': 'resolve_shipment', 'label': 'Resolve shipment'}],
            'edges': []}


def compiled_topology(compiled, graph_id, title):
    drawable = compiled.get_graph()
    return {'id': graph_id, 'title': title,
            'nodes': [{'id': key, 'label': key.replace('_', ' ').title()}
                      for key in drawable.nodes if key not in {'__start__', '__end__'}],
            'edges': [{'source': edge.source, 'target': edge.target}
                      for edge in drawable.edges
                      if edge.source not in {'__start__', '__end__'} and
                      edge.target not in {'__start__', '__end__'}]}


def external_topologies():
    email_graph = ShipmentEmailGraph(None, None, None, None).graph
    case = ShipmentCaseGraph(mail=None, model=None, wms=None,
                             carrier_read=lambda *_: None, evidence=None,
                             checkpointer=None)
    return [compiled_topology(email_graph, 'shipment_email', 'Shipment email graph'),
            compiled_topology(case.graph, 'shipment_case', 'Shipment case graph'),
            compiled_topology(case.investigator, 'shipment_investigator',
                              'Investigation subgraph'),
            compiled_topology(case.voice, 'shipment_voice', 'Voice subgraph')]


def topologies_for_execution(catalog, events):
    """Show compiled graphs plus capabilities that actually ran."""
    used = {event['graph_id'] for event in events}
    known = {item['id']: item for item in catalog}
    result = []
    for graph_id in known:
        if graph_id not in used:
            continue
        graph = {**known[graph_id], 'nodes': list(known[graph_id]['nodes']),
                 'edges': list(known[graph_id]['edges'])}
        present = {node['id'] for node in graph['nodes']}
        for event in events:
            if event['graph_id'] == graph_id and event['node'] not in present:
                node = event['node']
                graph['nodes'].append({'id': node,
                                       'label': node.replace('_', ' ').title()})
                present.add(node)
        result.append(graph)
    for graph_id in sorted(used - set(known)):
        nodes = list(dict.fromkeys(event['node'] for event in events
                                   if event['graph_id'] == graph_id))
        result.append({'id': graph_id,
                       'title': 'Invoked capabilities' if graph_id == 'capabilities'
                       else graph_id.replace('_', ' ').title(),
                       'nodes': [{'id': name, 'label': name.replace('_', ' ').title()}
                                 for name in nodes], 'edges': []})
    return result


CONTACT_LABELS = {'driver': 'driver', 'sending_warehouse': 'sending warehouse',
                  'shipment_company': 'shipment company'}
# The grounded request reader's intent, mapped to the capability it asks for.
INTENT_CAPABILITIES = {
    'action_items': 'list_action_items', 'delayed_shipments': 'list_delayed_shipments',
    'recall_alerts': 'list_recall_alerts',
    'recall_search': 'recall_search', 'recall_notify': 'recall_notify',
    'tow_search': 'tow_search', 'tow_request_quotes': 'tow_request_quotes',
    'tow_check_replies': 'tow_check_replies', 'tow_save_vendor': 'tow_save_vendor',
    'driver_check': 'request_contact_update', 'contact_check': 'request_contact_update',
    'backup_truck': 'arrange_backup_truck', 'book_inspection': 'book_cargo_inspection',
    'contact_reply_check': 'check_contact_replies',
    'data_question': 'query_openboxes_data',
    'purchase_orders': 'review_purchase_orders', 'request_blockers': 'diagnose_open_requests',
    'receiving_bins': 'review_receiving_bins', 'expiry_risk': 'review_expiry_risk',
    'bin_health': 'review_bin_health', 'supplier_chase': 'chase_supplier',
    'supplier_reply_check': 'check_supplier_reply',
    'apply_supplier_date': 'apply_supplier_delivery_date',
    'create_putaway': 'create_putaway_tasks', 'quarantine_expired': 'quarantine_expired_lots',
    'reorder': 'propose_purchase_orders', 'create_purchase_order': 'create_draft_purchase_orders'}
# Warehouse-level intents that name no shipment.
WAREHOUSE_INTENTS = {'action_items', 'recall_alerts', 'data_question', 'purchase_orders',
                     'request_blockers', 'receiving_bins', 'expiry_risk', 'bin_health',
                     'supplier_chase', 'supplier_reply_check', 'apply_supplier_date',
                     'create_putaway', 'quarantine_expired', 'reorder',
                     'create_purchase_order'}
QUOTE_CAPABILITIES = {
    'shipment_status': 'shipment_status', 'load_transfer_status': 'load_transfer_status',
    'focused_question': 'focused_question', 'check_quotes': 'check_quotes',
    'select_vendor': 'select_vendor', 'follow_up': 'follow_up_vendor'}


@dataclass
class ChatTurn:
    """One operator request: verified scope plus lazily read, cached evidence."""
    service: 'WorkflowService'
    store: OperatorChatStore
    model: object
    actor: Actor
    text: str
    history: list
    shipment_context: dict | None = None
    case_record: dict | None = None
    resolution_note: str | None = None
    intent_type: str | None = None
    contact_role: str | None = None
    tow_service_reference: str | None = None
    recall_context: dict | None = None
    attention_context: dict | None = None
    shipment_search_context: dict | None = None
    excluded_shipment_reference: str | None = None
    include_model_calls: bool = False
    has_selector: bool = False
    # Picks the chat component for a tool's data when more than one would fit.
    component_chooser: object = None
    data_ticket: str | None = None
    current: str | None = None
    resolved_attention: dict | None = None
    selection_errors: list = dataclass_field(default_factory=list)
    # The contact emailed during this turn; their reply cannot be in the thread yet.
    contact_requested: str | None = None
    # Warehouse operations facts read once per turn and shared by its capabilities.
    ops_snapshots: dict = dataclass_field(default_factory=dict)
    _quote_action: dict | None = None
    _contact_pending: bool | None = None

    def __post_init__(self):
        self.sla = sla_status(self.shipment_context)

    @property
    def shipment_id(self):
        return (self.shipment_context or {}).get('id')

    @property
    def shipment_number(self):
        return (self.shipment_context or {}).get('shipmentNumber') or 'this shipment'

    @property
    def target(self):
        return (self.shipment_id if self.shipment_id else
                'location:' + str(self.actor.location_id))

    @property
    def vendors(self):
        return configured_carriers((self.case_record or {}).get('outreach') or {})

    @property
    def selected_vendor_id(self):
        return ((((self.case_record or {}).get('recovery_quotes') or {}).get('selection') or
                 {}).get('selected') or {}).get('shipper_id')

    @property
    def session_id(self):
        return (self.case_record or {}).get('session_id')

    def add_sources(self, *sources):
        self.store.update_progress(self.actor.request_id, self.actor.user_id,
                                   self.actor.location_id, sources=list(sources))

    def quote_action(self):
        """Grounded reading of a quote or carrier request; raises if ungrounded."""
        if self._quote_action is None:
            if not self.case_record:
                self._quote_action = {'action': 'other'}
            else:
                self._quote_action, _ = interpret_quote_request(
                    self.model, self.text, self.history, self.vendors,
                    self.selected_vendor_id)
        return self._quote_action

    def suggested_capability(self, read_quote=False):
        if self.intent_type == 'contact_check' and \
                self.contact_role not in {'sending_warehouse', 'shipment_company'}:
            pass
        elif self.intent_type in INTENT_CAPABILITIES:
            return INTENT_CAPABILITIES[self.intent_type]
        if self.case_record and (read_quote or not self.has_selector or
                                 self._quote_action is not None):
            return QUOTE_CAPABILITIES.get(self.quote_action()['action'])
        return None

    def activity_for(self, capability):
        """The request reader's own wording of the step, when it described one."""
        if capability == 'request_contact_update' and self.intent_type == 'driver_check':
            return 'Preparing a driver status request'
        action = self._quote_action or {}
        if QUOTE_CAPABILITIES.get(action.get('action')) == capability:
            return action.get('activity')
        return None

    def contact_request_pending(self):
        """Whether our last contact email is unanswered and inside the follow-up window.

        Read from local records only, so suggestions never read the mailbox: every
        contact send is in the ledger, and replies are stored whenever a thread is read
        (by a reply check, a send, or the contact monitor). A reply that has not been
        read yet hides the suggestion until then; the send guard re-checks the mailbox.
        """
        if self._contact_pending is None:
            config = self.service.config
            if self.contact_requested:
                self._contact_pending = True
            elif not self.shipment_id:
                self._contact_pending = False
            else:
                window = config.contact_follow_up_hours * 3600
                ledger = ActionLedger(config.state_path)
                try:
                    sends = [ledger.last_sent(name, self.shipment_id, window) for name in
                             (CONTACT_SEND, 'request_contact_update', 'contact_auto_follow_up')]
                finally:
                    ledger.close()
                sent_at = max((parse_instant(item) for item in sends if item), default=None)
                store = ContactReplyStore(config.state_path)
                try:
                    replied_at = parse_instant(store.latest_received_at(self.shipment_id))
                finally:
                    store.close()
                self._contact_pending = bool(sent_at) and not (
                    replied_at and replied_at > sent_at)
        return self._contact_pending

    def contact_update_role(self):
        if self.intent_type == 'driver_check':
            return 'driver'
        if self.intent_type == 'contact_check' and self.contact_role in CONTACT_LABELS:
            return self.contact_role
        journal = OutreachJournal(self.service.config.state_path)
        try:
            previous = journal.request(self.shipment_id) if self.shipment_id else None
        finally:
            journal.close()
        return (previous or {}).get('role')


class WorkflowService:
    def __init__(self, config: Config, *, wms_factory=OpenBoxesTool,
                 jev_factory=JevRouter, model_factory=OpenRouter,
                 mail_factory=AgentMail, database_path=None, selector_factory=None,
                 component_chooser_factory=None, next_step_factory=None,
                 broker_assessor_factory=JevBrokerReplyAssessor,
                 inspection_assessor_factory=JevInspectionAssessor,
                 eta_assessor_factory=JevEtaAssessor, route_pages_factory=BrowserTowPages):
        self.config = config
        # Jev's comparison of quoted times with Google Maps, and the Maps browser pages.
        self.eta_assessor_factory = eta_assessor_factory
        self.route_pages_factory = route_pages_factory
        # Jev's reading of the cargo inspector's replies.
        self.inspection_assessor_factory = inspection_assessor_factory
        # Jev's reading of the selected broker's replies.
        self.broker_assessor_factory = broker_assessor_factory
        # Ranks suggested next steps after an answer; None suggests nothing.
        self.next_step_factory = next_step_factory
        # The chat's capability classifier; None follows the request reader only.
        self.selector_factory = selector_factory
        # Chooses how tool data is shown; None takes the first fitting component.
        self.component_chooser_factory = component_chooser_factory
        self.wms_factory = wms_factory
        self.jev_factory = jev_factory
        self.model_factory = model_factory
        self.mail_factory = mail_factory
        base = Path(database_path or (str(Path(config.state_path).resolve()) + '.workflow-ui.sqlite3'))
        base.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(base)
        self.checkpoint_path = str(base) + '.checkpoints.sqlite3'
        private_database_file(self.db_path)
        self.lock = threading.RLock()
        self.chat_lock = threading.RLock()
        self.quote_refresh_lock = threading.Lock()
        # One backup-truck run per shipment: chat and the monitor never draft together.
        self.backup_truck_locks = {}
        self.backup_truck_locks_guard = threading.Lock()
        self.quote_refresh_jobs = {}
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS workflow_sessions ('
                       'id TEXT PRIMARY KEY, shipment_id TEXT NOT NULL, '
                       'shipment TEXT NOT NULL, created_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_events ('
                       'session_id TEXT NOT NULL, sequence INTEGER NOT NULL, '
                       'namespace TEXT NOT NULL, node TEXT NOT NULL, payload TEXT NOT NULL, '
                       'PRIMARY KEY(session_id, sequence))')
            db.execute('CREATE TABLE IF NOT EXISTS shipment_case_state ('
                       'shipment_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, '
                       'shipment_number TEXT, state_json TEXT NOT NULL, '
                       'updated_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS workflow_carrier_emails ('
                       'session_id TEXT NOT NULL, message_id TEXT NOT NULL, '
                       'shipment_id TEXT NOT NULL, shipper_id TEXT NOT NULL, '
                       'thread_id TEXT NOT NULL, sender TEXT NOT NULL, subject TEXT, '
                       'received_at TEXT, body TEXT, body_sha256 TEXT NOT NULL, '
                       'body_truncated INTEGER NOT NULL, has_attachments INTEGER NOT NULL, '
                       'first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, '
                       'PRIMARY KEY(session_id,message_id))')
        self._backfill_case_projections()

    def _db(self):
        return sqlite3.connect(self.db_path, timeout=15)

    def _backfill_case_projections(self):
        with self._db() as db:
            sessions = db.execute('SELECT id FROM workflow_sessions '
                                  'ORDER BY created_at').fetchall()
        for (session_id,) in sessions:
            self._reconcile_load_transfer_state(session_id)
            self._refresh_case_projection(session_id)

    def _reconcile_load_transfer_state(self, session_id):
        """Bring older checkpoints forward from recorded operator email steps."""
        with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
            graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
            config = {'configurable': {'thread_id': session_id}}
            state = graph.get_state(config).values or {}
            if not (state.get('recovery_quotes') or {}).get('operator_choice'):
                return
            transfer = load_transfer_status(state)
            plan = state.get('fleet_plan') or {}
            if plan.get('load_transfer') != transfer:
                graph.update_state(config, {'fleet_plan': {
                    **plan, 'load_transfer': transfer}}, as_node='compose_reply')

    def _refresh_case_projection(self, session_id, record_incidents=False):
        """Rebuild the saved case; only a new driver message may write to OpenBoxes."""
        session = self._session(session_id)
        if not session:
            return
        with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
            graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
            state = graph.get_state({'configurable': {'thread_id': session_id}}).values or {}
        if not state.get('messages') and not state.get('recovery_quotes'):
            return
        if (state.get('shipment') or {}).get('id') != session['shipment_id']:
            return
        with self._db() as db:
            rows = db.execute('SELECT node,payload FROM workflow_events '
                              'WHERE session_id=? ORDER BY sequence',
                              (session_id,)).fetchall()
            events = [{'node': node, 'update': json.loads(payload)} for node, payload in rows]
            projection = case_projection(session_id, state, events)
            db.execute('INSERT INTO shipment_case_state VALUES (?,?,?,?,?) '
                       'ON CONFLICT(shipment_id) DO UPDATE SET '
                       'session_id=excluded.session_id, '
                       'shipment_number=excluded.shipment_number, '
                       'state_json=excluded.state_json, updated_at=excluded.updated_at',
                       (session['shipment_id'], session_id,
                        projection.get('shipment_number'),
                        json.dumps(projection, default=str), utcnow()))
        if record_incidents:
            self._record_incident_reports(session['shipment_id'], state, projection)

    def _record_incident_reports(self, shipment_id, state, projection):
        """Each driver-reported breakdown or accident becomes one OpenBoxes comment."""
        if (state.get('shipment') or {}).get('synthetic') is not True or \
                shipment_id == SAMPLE_SHIPMENT['id']:
            # The built-in sample has no OpenBoxes shipment to write to.
            return
        messages = {item.get('id'): item for item in state.get('messages') or []
                    if isinstance(item, dict)}
        pending = []
        ledger = ActionLedger(self.config.state_path)
        try:
            for item in projection.get('incident_history') or []:
                # One comment per incident type: a driver may repeat a report across
                # several incident chats, and each repeat is not a new incident.
                key = f'incident:{shipment_id}:{item.get("type")}'
                if not item.get('source_message_id') or \
                        ledger.count('openboxes_record', key, 'recorded'):
                    continue
                text = str((messages.get(item['source_message_id']) or {}).get('text') or '')
                when = (readable_instant(item['reported_at']) if item.get('reported_at')
                        else 'an unrecorded time')
                quote = '\n'.join('> ' + line for line in text.strip()[:1200].splitlines())
                article = 'an' if str(item['type'])[:1] in 'aeiou' else 'a'
                pending.append((key, f'The driver reported {article} {item["type"]} in the '
                                     'incident '
                                     f'chat on {when}' + (f':\n\n{quote}' if quote else '.')))
        finally:
            ledger.close()
        if pending:
            self._record_shipment_activity(lambda recorder: [
                recorder.record(shipment_id, key, comment) for key, comment in pending])

    @observed_capability('read_shipment_case')
    def operator_case_record(self, shipment_id):
        with self._db() as db:
            row = db.execute('SELECT state_json FROM shipment_case_state '
                             'WHERE shipment_id=?', (shipment_id,)).fetchone()
        if not row:
            session_id, _ = self._operator_quote_case(shipment_id)
            if session_id:
                self._refresh_case_projection(session_id)
                with self._db() as db:
                    row = db.execute('SELECT state_json FROM shipment_case_state '
                                     'WHERE shipment_id=?', (shipment_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _persist_carrier_email(self, session_id, shipment_id, item):
        """Store a verified carrier email before its content reaches the model."""
        message_id = item['message_id']
        body = item.get('extracted_text') or ''
        if not isinstance(body, str):
            body = ''
        digest = hashlib.sha256(body.encode()).hexdigest()
        stamp = utcnow()
        row = (session_id, message_id, shipment_id, item['shipper_id'],
               item['thread_id'], item.get('from') or '',
               str(item.get('subject') or '')[:500], item.get('timestamp'),
               body[:64000], digest, int(len(body) > 64000),
               int(bool(item.get('attachments'))), stamp, stamp)
        with self._db() as db:
            saved = db.execute('SELECT body_sha256,thread_id,shipper_id '
                               'FROM workflow_carrier_emails '
                               'WHERE session_id=? AND message_id=?',
                               (session_id, message_id)).fetchone()
            if saved and saved != (digest, item['thread_id'], item['shipper_id']):
                raise ValueError('Saved carrier email identity or content changed')
            if saved:
                db.execute('UPDATE workflow_carrier_emails SET last_seen_at=? '
                           'WHERE session_id=? AND message_id=?',
                           (stamp, session_id, message_id))
                return
            db.execute('INSERT INTO workflow_carrier_emails VALUES '
                       '(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', row)
            sequence = db.execute('SELECT COALESCE(MAX(sequence), 0) '
                                  'FROM workflow_events WHERE session_id=?',
                                  (session_id,)).fetchone()[0]
            db.execute('INSERT INTO workflow_events VALUES (?,?,?,?,?)',
                       (session_id, sequence + 1, json.dumps(['recovery_quotes']),
                        'persist_carrier_email', json.dumps({
                            'message_id': message_id, 'shipper_id': item['shipper_id'],
                            'thread_id': item['thread_id'],
                            'received_at': item.get('timestamp')})))

    def carrier_email_evidence(self, session_id):
        with self._db() as db:
            rows = db.execute('SELECT message_id,shipper_id,thread_id,sender,subject,'
                              'received_at,body,body_sha256,body_truncated,'
                              'has_attachments,first_seen_at,last_seen_at '
                              'FROM workflow_carrier_emails WHERE session_id=? '
                              'ORDER BY received_at,message_id', (session_id,)).fetchall()
        keys = ('message_id', 'shipper_id', 'thread_id', 'sender', 'subject',
                'received_at', 'body', 'body_sha256', 'body_truncated',
                'has_attachments', 'first_seen_at', 'last_seen_at')
        return [dict(zip(keys, row)) for row in rows]

    @observed_service('recovery_dispatch', 'selected_carrier_reply_check')
    def refresh_selected_carrier_report(self, session_id):
        """Read the selected thread and save its latest grounded vehicle report."""
        with self.lock:
            with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
                graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
                config = {'configurable': {'thread_id': session_id}}
                state = graph.get_state(config).values or {}
                review = state.get('recovery_quotes') or {}
                choice = review.get('operator_choice') or {}
                dispatch = review.get('dispatch_request') or {}
                if not choice or not dispatch.get('carrier_confirmation'):
                    return {'outcome': 'no_selected_carrier'}
                if not self.config.agentmail_key or not self.config.openrouter_key:
                    return {'outcome': 'not_configured'}
                progress_step('read_selected_carrier_thread', 'recovery_dispatch',
                              f'Reading {choice.get("vendor_name", "selected carrier")} replies '
                              f'for {(state.get("shipment") or {}).get("shipmentNumber", "shipment")}')
                mail = self.mail_factory(self.config.agentmail_key,
                                         self.config.agentmail_inbox)
                try:
                    replies = selected_carrier_replies(mail, dispatch, choice)
                    for item in replies:
                        self._persist_carrier_email(session_id, state['shipment']['id'],
                                                    {**item, 'shipper_id': choice['vendor_id']})
                finally:
                    mail.client.close()
                if not replies:
                    return {'outcome': 'awaiting_reply'}
                latest = replies[-1]
                if dispatch.get('carrier_report_checked_message_id') == latest['message_id']:
                    # Only a confirmed trip that failed to save is retried for the same reply.
                    return {'outcome': 'already_reviewed',
                            'carrier_report': dispatch.get('carrier_report'),
                            'next_step': dispatch.get('broker_next_step'),
                            'recovery_shipment': self._record_recovery_trip(session_id)}
                model = self.model_factory(self.config.openrouter_key,
                                           self.config.openrouter_model, capture_trace=True)
                model.single_json_attempt = True
                model.max_provider_attempts = 1
                model.request_timeout_seconds = 20
                try:
                    report = extract_carrier_vehicle_report(model, latest, {
                        'vendor_name': choice.get('vendor_name'),
                        'shipment_number': state['shipment'].get('shipmentNumber'),
                        'outbound_message_id': dispatch['carrier_confirmation']['message_id']})
                    calls = [{'node': 'extract_carrier_vehicle_report',
                              'provider': 'OpenRouter', **item}
                             for item in model.trace or []]
                finally:
                    model.client.close()
                # Jev chooses what happens next; a failed decision retries on the next check.
                assessor = self.broker_assessor_factory(self.config.openrouter_key)
                try:
                    decision = settled(assessor.decide(decision_context(state), latest))
                finally:
                    assessor.close()
                decision.update(message_id=latest['message_id'],
                                decided_at=datetime.now(timezone.utc).isoformat())
                updated_dispatch = {**dispatch,
                    'carrier_report_checked_message_id': latest['message_id'],
                    'broker_reply': report, 'broker_next_step': decision}
                if report['status'] != 'unknown':
                    updated_dispatch['carrier_report'] = report
                self._save_dispatch(graph, config, state, updated_dispatch, calls)
            self._log_event(session_id, 'carrier_vehicle_report', report)
            self._log_event(session_id, 'broker_next_step', decision)
            self._refresh_case_projection(session_id)
            return {'outcome': 'reviewed', 'carrier_report': report, 'next_step': decision,
                    **self._act_on_broker_step(session_id, decision, latest)}

    @staticmethod
    def _save_dispatch(graph, config, state, dispatch, calls=()):
        review = {**(state.get('recovery_quotes') or {}), 'dispatch_request': dispatch}
        updated_state = {**state, 'recovery_quotes': review}
        graph.update_state(config, {
            'recovery_quotes': review,
            'fleet_plan': {**(state.get('fleet_plan') or {}),
                           'load_transfer': load_transfer_status(updated_state)},
            'model_calls': [*state.get('model_calls', []), *calls]},
            as_node='compose_reply')

    def _log_event(self, session_id, kind, payload):
        with self._db() as db:
            sequence = db.execute('SELECT COALESCE(MAX(sequence), 0) '
                                  'FROM workflow_events WHERE session_id=?',
                                  (session_id,)).fetchone()[0]
            db.execute('INSERT INTO workflow_events VALUES (?,?,?,?,?)',
                       (session_id, sequence + 1, json.dumps(['recovery_dispatch']),
                        kind, json.dumps(payload, default=str)))

    def _act_on_broker_step(self, session_id, decision, reply):
        """Carry out Jev's choice; the code only applies gates and limits."""
        choice = decision['choice']
        if choice == 'create_recovery_shipment':
            return {'recovery_shipment': self._record_recovery_trip(session_id)}
        if choice == 'follow_up':
            return {'follow_up': self._follow_up_broker(session_id, reply)}
        if choice == 'hand_to_person':
            self._update_dispatch(session_id, lambda dispatch: {**dispatch, 'needs_person': {
                'reason': decision.get('reason') or 'broker_reply_needs_decision',
                'message_id': reply['message_id']}})
            return {'needs_person': True}
        return {}

    def _update_dispatch(self, session_id, change):
        with self.lock:
            with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
                graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
                config = {'configurable': {'thread_id': session_id}}
                state = graph.get_state(config).values or {}
                dispatch = (state.get('recovery_quotes') or {}).get('dispatch_request') or {}
                self._save_dispatch(graph, config, state, change(dispatch))
            self._refresh_case_projection(session_id)

    def _follow_up_broker(self, session_id, reply):
        """The model writes the follow-up; it is sent once per broker reply."""
        with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
            graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
            state = graph.get_state({'configurable': {'thread_id': session_id}}).values or {}
        mail = self.mail_factory(self.config.agentmail_key, self.config.agentmail_inbox)
        model = self.model_factory(self.config.openrouter_key, self.config.openrouter_model)
        journal = FleetOutreachJournal(self.config.state_path)
        try:
            result = send_broker_follow_up(mail, journal, model, state, reply)
        finally:
            journal.close()
            model.client.close()
            mail.client.close()

        def record(dispatch):
            updated = {**dispatch, 'broker_follow_ups': [
                *(dispatch.get('broker_follow_ups') or []), result]}
            if result['outcome'] == 'limit_reached':
                updated['needs_person'] = {'reason': 'broker_follow_up_limit_reached',
                                           'message_id': reply['message_id']}
            return updated
        self._update_dispatch(session_id, record)
        self._log_event(session_id, 'broker_follow_up', result)
        return result

    def sweep_due_recovery_quotes(self, *, now=None):
        """Start fair windows for legacy inquiries and close due quote rounds."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError('Quote monitor clock must include a time zone')
        now = now.astimezone(timezone.utc)
        due = []
        started = []
        awaiting_broker = []
        awaiting_truck = []
        awaiting_inspection = []
        awaiting_eta = []
        with self.lock:
            with self._db() as db:
                sessions = db.execute('SELECT id, shipment_id FROM workflow_sessions').fetchall()
            with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
                graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
                for session_id, shipment_id in sessions:
                    state = graph.get_state(
                        {'configurable': {'thread_id': session_id}}).values or {}
                    shipment = state.get('shipment') or {}
                    if shipment.get('synthetic') is not True or shipment.get('id') != shipment_id:
                        continue
                    inspection = (state.get('outreach') or {}).get('inspection') or {}
                    if inspection.get('outcome') in {'sent', 'already_sent'} and \
                            (state.get('inspection_review') or {}).get('state') not in {
                                'cleared', 'not_cleared', 'needs_person'}:
                        # The inspector's replies drive the inspection loop on their own.
                        awaiting_inspection.append(session_id)
                    outreach = (state.get('outreach') or {}).get('backup_shippers') or {}
                    quotes_requested = any(
                        item.get('outcome') in {'sent', 'already_sent'} and
                        item.get('thread_id') for item in outreach.get('results') or [])
                    if state.get('backup_truck_request') and not quotes_requested:
                        # The operator asked for a backup truck; a driver reply with the
                        # missing location lets the quotes go out without asking again.
                        if shipment_id not in awaiting_truck:
                            awaiting_truck.append(shipment_id)
                        continue
                    if not quotes_requested:
                        continue
                    review = state.get('recovery_quotes') or {}
                    dispatch = review.get('dispatch_request') or {}
                    if (review.get('operator_choice') or {}).get('vendor_id') and \
                            (dispatch.get('carrier_confirmation') or {}).get('outcome') in {
                                'sent', 'already_sent'} and \
                            not (review.get('recovery_shipment') or {}).get('id'):
                        awaiting_broker.append(session_id)
                        continue
                    window = review.get('quote_window') or {}
                    if window.get('status') == 'closed' and \
                            not (review.get('operator_choice') or {}).get('vendor_id') and \
                            any(check.get('step') == 'ask_carrier' for check in
                                (review.get('eta_checks') or {}).values()):
                        # We asked a carrier about its times; its answer can change the pick.
                        awaiting_eta.append(session_id)
                        continue
                    if window and window.get('status') != 'open':
                        continue
                    deadline = window.get('deadline_at') if window else outreach.get('deadline_at')
                    selection_outcome = (review.get('selection') or {}).get('outcome')
                    if not deadline and not window and selection_outcome in {
                            None, 'collecting_quotes'}:
                        deadline = (now + timedelta(
                            minutes=CONDITIONAL_QUOTE_WINDOW_MINUTES)).isoformat().replace(
                                '+00:00', 'Z')
                        new_window = {'started_at': now.isoformat().replace('+00:00', 'Z'),
                                      'deadline_at': deadline, 'status': 'open',
                                      'basis': 'legacy_monitor_started'}
                        graph.update_state(
                            {'configurable': {'thread_id': session_id}},
                            {'recovery_quotes': {**review, 'quote_window': new_window}},
                            as_node='compose_reply')
                        started.append((session_id, new_window))
                        continue
                    try:
                        instant = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
                    except (AttributeError, ValueError):
                        continue
                    if instant.tzinfo is not None and now >= instant.astimezone(timezone.utc):
                        due.append(session_id)
            if started:
                with self._db() as db:
                    for session_id, window in started:
                        sequence = db.execute(
                            'SELECT COALESCE(MAX(sequence), 0) FROM workflow_events '
                            'WHERE session_id=?', (session_id,)).fetchone()[0]
                        db.execute('INSERT INTO workflow_events VALUES (?,?,?,?,?)',
                                   (session_id, sequence + 1,
                                    json.dumps(['recovery_quotes']),
                                    'quote_window_started', json.dumps(window)))
        result = {'checked_at': now.isoformat().replace('+00:00', 'Z'),
                  'started': len(started), 'due': len(due), 'closed': 0,
                  'recovery_shipments_created': 0, 'errors': []}
        for session_id in awaiting_eta:
            try:
                self.check_recovery_replies(session_id, apply=True)
            except Exception as error:
                result['errors'].append({'session_id': session_id, 'step': 'eta_answer',
                                         'error_type': type(error).__name__})
        for session_id in awaiting_inspection:
            try:
                self.advance_inspection(session_id)
            except Exception as error:
                result['errors'].append({'session_id': session_id, 'step': 'inspection',
                                         'error_type': type(error).__name__})
        for shipment_id in awaiting_truck:
            try:
                self.arrange_backup_truck(shipment_id, requested_by='monitor', wait=False)
            except Exception as error:
                result['errors'].append({'shipment_id': shipment_id,
                                         'error_type': type(error).__name__})
        # A broker's confirmation email creates the recovery shipment without an operator.
        for session_id in awaiting_broker:
            try:
                checked = self.refresh_selected_carrier_report(session_id)
            except Exception as error:
                result['errors'].append({'session_id': session_id,
                                         'error_type': type(error).__name__})
                continue
            if (checked.get('recovery_shipment') or {}).get('outcome') == 'created':
                result['recovery_shipments_created'] += 1
        for session_id in due:
            with self.quote_refresh_lock:
                if (self.quote_refresh_jobs.get(session_id) or {}).get('status') == 'running':
                    continue
                self.quote_refresh_jobs[session_id] = {
                    'status': 'running', 'started_monotonic': time.monotonic()}
            try:
                self.check_recovery_replies(session_id, apply=True)
                result['closed'] += 1
                status = 'completed'
            except Exception as error:
                result['errors'].append({'session_id': session_id,
                                         'error_type': type(error).__name__})
                status = 'failed'
            with self.quote_refresh_lock:
                self.quote_refresh_jobs[session_id]['status'] = status
        return result

    def _wms(self):
        wms = self.wms_factory(self.config)
        wms.login()
        return wms

    def _coordinate_recovery(self, state):
        if state['shipment'].get('synthetic') is not True:
            return {'inspection': {'outcome': 'review',
                                   'reason': 'synthetic_shipment_required'},
                    'backup_shippers': {'outcome': 'review',
                                        'reason': 'synthetic_shipment_required'}}
        if not self.config.agentmail_key:
            return {'inspection': {'outcome': 'not_configured'},
                    'backup_shippers': {'outcome': 'not_configured'}}
        mail = self.mail_factory(self.config.agentmail_key, self.config.agentmail_inbox)
        journal = FleetOutreachJournal(self.config.state_path)
        writer = self.model_factory(self.config.openrouter_key, self.config.openrouter_model)
        try:
            return coordinate_incident_outreach(state, mail, journal, apply=True,
                                                writer=writer)
        finally:
            journal.close()
            mail.client.close()
            writer.client.close()

    def coordinate_existing_session(self, session_id):
        """Apply test recovery outreach to a saved case created before this node existed."""
        with self.lock:
            if not self._session(session_id):
                raise UnknownSessionError('Unknown session')
            with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
                workflow = IncidentWorkflow(None, None, checkpointer=checkpointer)
                config = {'configurable': {'thread_id': session_id}}
                state = workflow.graph.get_state(config).values or {}
                if not state.get('fleet_plan') or not state.get('decisions'):
                    raise ValueError('This case has no completed incident assessment')
                old_outreach = state.get('outreach') or {}
                old_inspection = (old_outreach.get('inspection') or {}).get('outcome')
                old_shippers = (old_outreach.get('backup_shippers') or {}).get('results') or []
                if (old_inspection in {'sent', 'already_sent'} and old_shippers and
                        all(item.get('outcome') in {'sent', 'already_sent'}
                            for item in old_shippers)):
                    return self.snapshot(session_id)
                outreach = self._coordinate_recovery(state)
                update = {'outreach': outreach}
                workflow.graph.update_state(config, update, as_node='compose_reply')
            with self._db() as db:
                sequence = db.execute('SELECT COALESCE(MAX(sequence), 0) FROM workflow_events '
                                      'WHERE session_id=?', (session_id,)).fetchone()[0]
                db.execute('INSERT INTO workflow_events VALUES (?,?,?,?,?)',
                           (session_id, sequence + 1, json.dumps([]),
                            'coordinate_recovery', json.dumps(update, default=str)))
            self._refresh_case_projection(session_id)
            return self.snapshot(session_id)

    @observed_service('recovery_quotes', 'carrier_email_check')
    def check_recovery_replies(self, session_id, *, apply=False):
        """Read conditional carrier threads and checkpoint the quote review."""
        with self.lock:
            if not self._session(session_id):
                raise UnknownSessionError('Unknown session')
            with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
                workflow = IncidentWorkflow(None, None, checkpointer=checkpointer)
                checkpoint_config = {'configurable': {'thread_id': session_id}}
                state = workflow.graph.get_state(checkpoint_config).values or {}
                if not (state.get('outreach') or {}).get('backup_shippers'):
                    raise ValueError('This case has no conditional carrier inquiry')
                if not self.config.agentmail_key or not self.config.openrouter_key:
                    raise RuntimeError('AgentMail and OpenRouter must be configured')
                mail = self.mail_factory(self.config.agentmail_key,
                                         self.config.agentmail_inbox)
                model = self.model_factory(self.config.openrouter_key,
                                           self.config.openrouter_model,
                                           capture_trace=True)
                # A quote review may examine several carrier threads. Keep each
                # extraction bounded so a stalled provider cannot hold the round open.
                model.single_json_attempt = True
                model.max_provider_attempts = 1
                model.request_timeout_seconds = 20
                journal = FleetOutreachJournal(self.config.state_path)
                try:
                    graph_result = recovery_review_graph(
                        mail, journal, model,
                        persist_email=lambda item: self._persist_carrier_email(
                            session_id, state['shipment']['id'], item)).invoke({
                        'shipment': state['shipment'], 'outreach': state['outreach'],
                        'fleet_plan': state.get('fleet_plan') or {},
                        'previous': state.get('recovery_quotes'), 'apply': apply,
                        'loop': state.get('agent_loop') or {}},
                        config=graph_config(graph_id='recovery_quotes'))
                    review = graph_result['review']
                finally:
                    journal.close()
                    mail.client.close()
                    model.client.close()
                new_calls = review.pop('model_calls')
                review['operator_followups'] = (state.get('recovery_quotes') or {}).get(
                    'operator_followups') or []
                review['operator_choice'] = (state.get('recovery_quotes') or {}).get(
                    'operator_choice') or {}
                review['automated_selection'] = (state.get('recovery_quotes') or {}).get(
                    'automated_selection') or {}
                loop = graph_result.get('loop') or state.get('agent_loop') or {}
                if review != state.get('recovery_quotes') or new_calls or \
                        loop != state.get('agent_loop'):
                    update = {'recovery_quotes': review,
                              'agent_loop': loop,
                              'model_calls': [*state.get('model_calls', []), *new_calls]}
                    workflow.graph.update_state(checkpoint_config, update,
                                                as_node='compose_reply')
                    with self._db() as db:
                        sequence = db.execute(
                            'SELECT COALESCE(MAX(sequence), 0) FROM workflow_events '
                            'WHERE session_id=?', (session_id,)).fetchone()[0]
                        db.execute('INSERT INTO workflow_events VALUES (?,?,?,?,?)',
                                   (session_id, sequence + 1,
                                    json.dumps(['recovery_quotes']),
                                    'review_recovery_quotes',
                                    json.dumps(update, default=str)))
            self._refresh_case_projection(session_id)
            if apply and not (review.get('operator_choice') or {}).get('vendor_id') and \
                    (review.get('quote_window') or {}).get('status') == 'closed':
                try:
                    self.verify_quote_etas(session_id)
                except Exception:
                    # The quote review stands if the route check cannot run.
                    pass
            if (review.get('operator_choice') or {}).get('vendor_id'):
                try:
                    self.refresh_selected_carrier_report(session_id)
                except Exception:
                    # The quote review remains useful if dispatch extraction fails.
                    pass
            return self.snapshot(session_id)

    @observed_service('eta_verification', 'quote_eta_check')
    def verify_quote_etas(self, session_id):
        """Check each complete quote's times on Google Maps and act on what Jev decides."""
        state = self._incident_state(session_id)
        review = state.get('recovery_quotes') or {}
        if (review.get('operator_choice') or {}).get('vendor_id'):
            return {'outcome': 'carrier_already_chosen'}
        checks = dict(review.get('eta_checks') or {})
        candidates = [quote for quote in (_complete_candidate(item) for item in
                      review.get('shippers') or [] if item.get('selection_eligible'))
                      if quote]
        terms_by_shipper = {item.get('shipper_id'): item.get('terms') or {}
                            for item in review.get('shippers') or []}
        pending = [quote for quote in candidates if quote['quote_id'] not in checks]
        shipment = state['shipment']
        pickup = ((state.get('facts') or {}).get('roadside_location') or {}).get('value')
        destination = place_for(shipment.get('destination'))
        if pending and pickup and destination:
            model = self.model_factory(self.config.openrouter_key,
                                       self.config.openrouter_model)
            assessor = self.eta_assessor_factory(self.config.openrouter_key)
            pages = self.route_pages_factory(self.config.browser_path, self.config.cdp_url)
            mail = self.mail_factory(self.config.agentmail_key, self.config.agentmail_inbox)
            journal = FleetOutreachJournal(self.config.state_path)
            cache = {}
            try:
                with pages:
                    for quote in pending:
                        start, basis = truck_start(terms_by_shipper.get(quote['shipper_id']),
                                                   quote['shipper_id'])
                        to_pickup = route(model, pages, start, pickup, cache) if start else None
                        to_destination = route(model, pages, pickup, destination, cache)
                        facts = check_facts(quote, start, basis, pickup, destination,
                                            to_pickup, to_destination)
                        asked = any(check.get('shipper_id') == quote['shipper_id'] and
                                    check.get('step') == 'ask_carrier'
                                    for check in checks.values())
                        try:
                            decision = assessor.decide(facts)
                        except Exception as error:
                            decision = {'choice': 'plausible', 'confidence': 0.0,
                                        'error_type': type(error).__name__}
                        step = settle(decision, facts, asked) if (
                            to_pickup or to_destination) else 'unverified'
                        check = {'quote_id': quote['quote_id'],
                                 'shipper_id': quote['shipper_id'], 'facts': facts,
                                 'routes': [item for item in (to_pickup, to_destination)
                                            if item],
                                 'jev': decision, 'step': step, 'checked_at': utcnow()}
                        if step == 'ask_carrier':
                            try:
                                email = write_eta_question(model, shipment, quote, facts)
                                check['question'] = reply_on_thread(
                                    mail, journal, question_key(shipment['id'],
                                                                quote['quote_id']),
                                    {'message_id': quote['message_id'],
                                     'thread_id': quote['thread_id']}, email)
                            except Exception as error:
                                # Without a sent question the quote is ranked on Maps time.
                                check['step'] = 'use_maps_eta' if \
                                    'maps_hours_to_delivery' in facts else 'unverified'
                                check['question'] = {'outcome': 'draft_unavailable',
                                                     'error_type': type(error).__name__}
                        checks[quote['quote_id']] = check
            finally:
                journal.close()
                mail.client.close()
                assessor.close()
                model.client.close()
        late_fee = ((state.get('fleet_plan') or {}).get('load_transfer') or {}).get(
            'late_delivery_fee_usd') or shipment.get('late_delivery_fee_usd') or 0
        usable = effective_quotes(candidates, checks)
        before = (review.get('selection') or {}).get('selected') or {}
        selection = select_quote(usable, late_fee) if usable else {
            'outcome': 'review', 'reason': 'no_quote_passed_eta_check'
            if candidates else 'no_complete_quotes_at_cutoff'}
        after = selection.get('selected') or {}
        changes = list(review.get('selection_changes') or [])
        if before.get('quote_id') != after.get('quote_id'):
            changes.append({'from': before.get('shipper'), 'to': after.get('shipper'),
                            'because': 'Google Maps route check', 'at': utcnow(),
                            'checks': {quote_id: check['step']
                                       for quote_id, check in checks.items()}})

        def change(current):
            saved = current.get('recovery_quotes') or {}
            if (saved.get('operator_choice') or {}).get('vendor_id'):
                return {}
            return {'recovery_quotes': {**saved, 'eta_checks': checks,
                                        'selection': selection,
                                        'selection_changes': changes}}
        self._save_inspection(session_id, change)
        self._log_event(session_id, 'quote_eta_check', {
            'checks': {quote_id: check['step'] for quote_id, check in checks.items()},
            'selected': after.get('shipper')})
        return {'outcome': 'checked', 'checks': checks, 'selected': after.get('shipper'),
                'changed': before.get('quote_id') != after.get('quote_id')}

    @observed_service('recovery_quotes', 'dispatch_confirmation_request')
    def request_recovery_dispatch(self, session_id, approval):
        """Ask the chosen test carrier to confirm dispatch after fleet approval."""
        with self.lock:
            session = self._session(session_id)
            if not session:
                raise UnknownSessionError('Unknown session')
            with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
                workflow = IncidentWorkflow(None, None, checkpointer=checkpointer)
                checkpoint_config = {'configurable': {'thread_id': session_id}}
                state = workflow.graph.get_state(checkpoint_config).values or {}
                if (state.get('shipment') or {}).get('id') != session['shipment_id']:
                    raise ValueError('Shipment identity mismatch in saved case')
                writer = self.model_factory(self.config.openrouter_key,
                                            self.config.openrouter_model)
                try:
                    proposal = prepare_dispatch_request(state, approval, writer)
                finally:
                    writer.client.close()
                if not self.config.agentmail_key:
                    raise RuntimeError('AgentMail must be configured')
                mail = self.mail_factory(self.config.agentmail_key,
                                         self.config.agentmail_inbox)
                journal = FleetOutreachJournal(self.config.state_path)
                try:
                    result = send_dispatch_request(mail, journal, proposal)
                finally:
                    journal.close()
                    mail.client.close()
                if result['outcome'] not in {'sent', 'already_sent'}:
                    raise ValueError('Dispatch confirmation request was not sent: ' +
                                     result.get('reason', result['outcome']))
                review = {**state['recovery_quotes'],
                          'dispatch_request': {**(state['recovery_quotes'].get(
                              'dispatch_request') or {}),
                              'state': 'awaiting_carrier_confirmation', **result}}
                updated_state = {**state, 'recovery_quotes': review}
                update = {'recovery_quotes': review,
                          'fleet_plan': {**(state.get('fleet_plan') or {}),
                                         'load_transfer': load_transfer_status(updated_state)}}
                workflow.graph.update_state(checkpoint_config, update,
                                            as_node='compose_reply')
                with self._db() as db:
                    sequence = db.execute(
                        'SELECT COALESCE(MAX(sequence), 0) FROM workflow_events '
                        'WHERE session_id=?', (session_id,)).fetchone()[0]
                    db.execute('INSERT INTO workflow_events VALUES (?,?,?,?,?)',
                               (session_id, sequence + 1, json.dumps(['recovery_quotes']),
                                'request_carrier_dispatch',
                                json.dumps(update, default=str)))
            self._refresh_case_projection(session_id)
            return self.snapshot(session_id)

    def list_shipments(self, query=''):
        wms = self._wms()
        try:
            rows, offset = [], 0
            while offset < 1000:
                page = wms.request('GET', 'api/shipments',
                                   params={'max': 100, 'offset': offset})
                rows.extend(page)
                if len(page) < 100:
                    break
                offset += len(page)
            q = query.casefold().strip()
            return [{'id': row['id'], 'shipment_number': row.get('shipmentNumber'),
                     'origin': row.get('origin'), 'destination': row.get('destination'),
                     'status': row.get('status'), 'synthetic': row.get('synthetic', False)}
                    for row in rows if not q or q in str(row.get('shipmentNumber', '')).casefold()
                    or q in str(row.get('id', '')).casefold()][:50]
        finally:
            wms.close()

    def create_session(self, shipment_id):
        wms = self._wms()
        try:
            shipment = wms.get_shipment_history(shipment_id)['shipment']
        finally:
            wms.close()
        if shipment.get('id') != shipment_id:
            raise ValueError('Shipment identity mismatch')
        return self._persist_session(shipment)

    @observed_service('shipment_resolution', 'chat_intake')
    def create_session_from_message(self, text):
        if not self.config.openrouter_key:
            raise RuntimeError('OPENROUTER_API_KEY is not configured')
        model = self.model_factory(self.config.openrouter_key,
                                   self.config.openrouter_model)
        try:
            entity, model_call = extract_shipment_entity(model, text)
        finally:
            model.client.close()
        reference = entity['shipment_reference']
        if not reference:
            raise ValueError('Include the shipment number or ID in your first update.')
        matches = [shipment for shipment in self.list_shipments(reference)
                   if reference.casefold() in {
                       str(shipment['id']).casefold(),
                       str(shipment.get('shipment_number') or '').casefold()}]
        if len(matches) != 1:
            raise ValueError('That shipment number or ID could not be uniquely verified in OpenBoxes.')
        session = self.create_session(matches[0]['id'])
        with self._db() as db:
            db.execute('INSERT INTO workflow_events VALUES (?,?,?,?,?)',
                       (session['session']['id'], 1, json.dumps(['shipment_resolution']),
                        'resolve_shipment', json.dumps({
                            'entity': entity, 'model_call': model_call}, default=str)))
        return self.snapshot(session['session']['id'])

    def create_sample_session(self):
        return self._persist_session(SAMPLE_SHIPMENT)

    def _persist_session(self, shipment):
        session_id = str(uuid.uuid4())
        with self._db() as db:
            db.execute('INSERT INTO workflow_sessions VALUES (?,?,?,?)',
                       (session_id, shipment['id'], json.dumps(shipment), utcnow()))
        return self.snapshot(session_id)

    def _session(self, session_id):
        try:
            if str(uuid.UUID(session_id)) != session_id:
                return None
        except (ValueError, AttributeError):
            return None
        with self._db() as db:
            row = db.execute('SELECT shipment_id, shipment, created_at FROM workflow_sessions '
                             'WHERE id=?', (session_id,)).fetchone()
        return {'id': session_id, 'shipment_id': row[0], 'shipment': json.loads(row[1]),
                'created_at': row[2]} if row else None

    @staticmethod
    def _event_parts(event):
        if isinstance(event, tuple) and len(event) == 2:
            namespace, update = event
        else:
            namespace, update = (), event
        namespace = list(namespace or ())
        if isinstance(update, dict) and update:
            node, payload = next(iter(update.items()))
        else:
            node, payload = 'unknown', update
        return namespace, node, payload

    def validate_message(self, session_id, text, image_data_url=None):
        session = self._session(session_id)
        if not session:
            raise UnknownSessionError('Unknown session')
        if not self.config.openrouter_key:
            raise RuntimeError('OPENROUTER_API_KEY is not configured')
        image_input = decode_image(image_data_url)
        if not text.strip():
            raise ValueError('Message is empty')
        return session, image_input

    @observed_service_stream('incident', 'console_chat')
    def stream_message(self, session_id, text, image_data_url=None, *, source=None):
        """source is a verified driver email: its own ID and send time are kept, so
        relative dates resolve from when the driver wrote, not when this runs."""
        with self.lock:
            session, image_input = self.validate_message(session_id, text, image_data_url)
            message = {'id': str(uuid.uuid4()), 'role': 'driver',
                       'text': text.strip(), 'timestamp': utcnow()}
            if source:
                message.update(id=source['message_id'], timestamp=source['timestamp'],
                               channel='email')
            yield 'message_received', {'message': message}
            jev = self.jev_factory(self.config.openrouter_key, capture_trace=True)
            model = self.model_factory(self.config.openrouter_key,
                                       self.config.openrouter_model, capture_trace=True)
            model.image_model = self.config.image_model
            try:
                image_observation = None
                image_updates = []
                image_calls = []
                if image_input:
                    yield 'node_started', {'namespace': ['image_analysis'], 'node': 'inspect_image'}
                    image, mime_type = image_input
                    image_observation = ShipmentImageGraph(model).analyze(
                        image, mime_type,
                        source={'kind': 'driver_chat', 'message_id': message['id'],
                                'captured_at': message['timestamp']})
                    message['image'] = {'sha256': image_observation['sha256'],
                                        'mime_type': mime_type}
                    image_updates.append((['image_analysis'], 'inspect_image',
                                          {'result': image_observation}))
                    image_calls = [{'node': 'inspect_image', 'provider': 'OpenRouter vision',
                                    **entry} for entry in (model.trace or [])]
                with self._db() as db:
                    sequence = db.execute('SELECT COALESCE(MAX(sequence), 0) FROM workflow_events '
                                          'WHERE session_id=?', (session_id,)).fetchone()[0]

                def completed(namespace, node, payload):
                    nonlocal sequence
                    sequence += 1
                    with self._db() as db:
                        db.execute('INSERT INTO workflow_events VALUES (?,?,?,?,?)',
                                   (session_id, sequence, json.dumps(namespace), node,
                                    json.dumps(payload, default=str)))
                    return {'sequence': sequence, 'namespace': namespace, 'node': node,
                            'update': payload}

                for namespace, node, payload in image_updates:
                    yield 'node_completed', completed(namespace, node, payload)

                with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
                    workflow = IncidentWorkflow(
                        jev, model, checkpointer=checkpointer,
                        outreach=self._coordinate_recovery)
                    config = {'configurable': {'thread_id': session_id}}
                    previous = workflow.graph.get_state(config).values or {}
                    initial = {'session_id': session_id, 'shipment': session['shipment'],
                               'new_message': message,
                               'new_image_observation': image_observation}
                    if image_calls:
                        initial['model_calls'] = [*previous.get('model_calls', []),
                                                  *image_calls]
                    for event in workflow.graph.stream(
                            initial, config=graph_config(config, 'incident'),
                            stream_mode=['tasks', 'updates'], subgraphs=True):
                        namespace, mode, data = event
                        namespace = list(namespace or ())
                        if mode == 'tasks':
                            if 'result' not in data:
                                yield 'node_started', {'namespace': namespace,
                                                       'node': data['name']}
                        else:
                            _, node, payload = self._event_parts((namespace, data))
                            if node not in {'__start__', '__end__'}:
                                yield 'node_completed', completed(namespace, node, payload)
                    state = workflow.graph.get_state(config).values
                    if not state.get('assistant_reply'):
                        raise RuntimeError('Workflow produced no reply')
            finally:
                jev.close()
                model.client.close()
            # A driver message may report a new incident; record it on the shipment.
            self._refresh_case_projection(session_id, record_incidents=True)
            yield 'complete', self.snapshot(session_id)

    def send_message(self, session_id, text, image_data_url=None, *, source=None):
        result = None
        for kind, payload in self.stream_message(session_id, text, image_data_url,
                                                 source=source):
            if kind == 'complete':
                result = payload
        return result

    def snapshot(self, session_id):
        session = self._session(session_id)
        if not session:
            raise UnknownSessionError('Unknown session')
        with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
            workflow = IncidentWorkflow(None, None, checkpointer=checkpointer)
            saved = workflow.graph.get_state({'configurable': {'thread_id': session_id}})
            state = saved.values or {'session_id': session_id, 'shipment': session['shipment'],
                                     'messages': [], 'status': 'waiting', 'model_calls': []}
            if (state.get('recovery_quotes') or {}).get('operator_choice'):
                state = {**state, 'fleet_plan': {**(state.get('fleet_plan') or {}),
                    'load_transfer': load_transfer_status(state)}}
            topology = workflow.topology()
            topology.append(image_topology())
            topology.append(recovery_review_topology())
            topology.extend(external_topologies())
            topology.append(operator_chat_topology())
            topology.append(operator_dispatch_topology())
            topology.append(shipment_resolution_topology())
        with self._db() as db:
            rows = db.execute('SELECT sequence, namespace, node, payload FROM workflow_events '
                              'WHERE session_id=? ORDER BY sequence', (session_id,)).fetchall()
        events = [{'sequence': row[0],
                   'namespace': (json.loads(row[1]) or ['recovery_quotes'])
                   if row[2] == 'review_recovery_quotes' else json.loads(row[1]),
                   'node': row[2],
                   'update': json.loads(row[3])} for row in rows]
        resolution_calls = [event['update']['model_call'] for event in events
                            if event['node'] == 'resolve_shipment' and
                            'model_call' in event['update']]
        return {'session': {key: session[key] for key in ('id', 'shipment_id', 'created_at')},
                'state': state, 'events': events, 'graphs': topology,
                'carrier_emails': self.carrier_email_evidence(session_id),
                'model_calls': resolution_calls + (state.get('model_calls') or [])}

    def graphs(self):
        with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
            topology = IncidentWorkflow(None, None, checkpointer=checkpointer).topology()
        topology.append(image_topology())
        topology.append(recovery_review_topology())
        topology.extend(external_topologies())
        topology.append(operator_chat_topology())
        topology.append(operator_dispatch_topology())
        topology.append(shipment_resolution_topology())
        return topology

    def execution_runs(self, limit=30):
        return ExecutionStore(self.config.state_path).list_runs(limit)

    def execution(self, run_id):
        details = ExecutionStore(self.config.state_path).read(run_id)
        if not details:
            raise UnknownSessionError('Unknown execution')
        details['graphs'] = topologies_for_execution(self.graphs(), details['events'])
        return details

    def operator_history(self, user_id, location_id):
        store = OperatorChatStore(self.config.state_path)
        return {'messages': self._hydrate_legacy_shipment_cards(
            store, user_id, location_id, store.history(user_id, location_id)),
            'recall_notices': RecallChatNoticeStore(
                self.config.state_path).for_warehouse(location_id)}

    def _hydrate_legacy_shipment_cards(self, store, user_id, location_id, messages):
        def needs_update(card):
            if card.get('type') != 'delayed_shipment' or not card.get('shipment_id'):
                return False
            number = card.get('number') or ''
            return not card.get('actions') or any(
                number and number in str(action.get('prompt') or '')
                for action in card.get('actions') or [] if isinstance(action, dict))

        missing = [card for message in messages for card in message.get('cards') or []
                   if needs_update(card)]
        if not missing:
            return messages
        try:
            context = self.delayed_card_context({'delayedShipments': [
                {'id': card['shipment_id']} for card in missing]})
        except Exception:
            return messages
        for message in messages:
            changed = False
            for card in message.get('cards') or []:
                shipment_id = card.get('shipment_id')
                if not needs_update(card) or shipment_id not in context:
                    continue
                card['actions'] = shipment_card_actions(
                    card.get('number') or 'Shipment', **context[shipment_id])
                changed = True
            if changed:
                store.save_cards(user_id, location_id, message['id'], message['cards'])
        return messages

    def _operator_quote_case(self, shipment_id):
        with self._db() as db:
            rows = db.execute('SELECT id FROM workflow_sessions WHERE shipment_id=? '
                              'ORDER BY created_at DESC', (shipment_id,)).fetchall()
        with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
            graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
            for (session_id,) in rows:
                state = graph.get_state({'configurable': {'thread_id': session_id}}).values or {}
                if (state.get('shipment') or {}).get('id') == shipment_id and \
                        (state.get('shipment') or {}).get('synthetic') is True and \
                        configured_carriers(state.get('outreach') or {}):
                    return session_id, state
        return None, None

    def _schedule_quote_refresh(self, session_id):
        """Refresh vendor email in the background without holding the chat response."""
        with self.quote_refresh_lock:
            prior = self.quote_refresh_jobs.get(session_id) or {}
            if prior.get('status') == 'running':
                return 'running'
            if time.monotonic() - prior.get('started_monotonic', 0) < 300:
                return prior.get('status') or 'recent'
            self.quote_refresh_jobs[session_id] = {
                'status': 'running', 'started_monotonic': time.monotonic()}

        def refresh():
            try:
                self.check_recovery_replies(session_id, apply=False)
                status = 'completed'
            except Exception:
                status = 'failed'
            with self.quote_refresh_lock:
                self.quote_refresh_jobs[session_id]['status'] = status

        threading.Thread(target=refresh, name='quote-refresh-' + session_id[:8],
                         daemon=True).start()
        return 'started'

    def _record_operator_followups(self, session_id, results):
        if not results:
            return
        with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
            graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
            config = {'configurable': {'thread_id': session_id}}
            state = graph.get_state(config).values or {}
            outreach = state.get('outreach') or {}
            backup = outreach.get('backup_shippers') or {}
            prior_threads = backup.get('followup_threads') or []
            prior_review = state.get('recovery_quotes') or {}
            prior_actions = prior_review.get('operator_followups') or []
            new_results = [item for item in results if item.get('outcome') in {
                'sent', 'already_sent'} and item.get('journal_key') not in {
                    earlier.get('journal_key') for earlier in prior_actions}]
            if not new_results:
                return
            email_threads = [item for item in new_results if item.get('channel') == 'email']
            update = {'outreach': {**outreach, 'backup_shippers': {
                **backup, 'followup_threads': [*prior_threads, *email_threads]}},
                'recovery_quotes': {**prior_review,
                                    'operator_followups': [*prior_actions, *new_results]}}
            graph.update_state(config, update, as_node='compose_reply')
        with self._db() as db:
            sequence = db.execute('SELECT COALESCE(MAX(sequence), 0) FROM workflow_events '
                                  'WHERE session_id=?', (session_id,)).fetchone()[0]
            db.execute('INSERT INTO workflow_events VALUES (?,?,?,?,?)',
                       (session_id, sequence + 1, json.dumps(['recovery_quotes']),
                        'operator_quote_followup', json.dumps(new_results)))
        self._refresh_case_projection(session_id)

    @observed_capability('record_operator_choice')
    def _record_operator_choice(self, session_id, vendor, user_id, action):
        with self.lock:
            with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
                graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
                config = {'configurable': {'thread_id': session_id}}
                state = graph.get_state(config).values or {}
                review = state.get('recovery_quotes') or {}
                item = next((entry for entry in review.get('shippers') or []
                             if entry.get('shipper_id') == vendor['id']), None)
                candidate = _complete_candidate(item or {})
                if not candidate or item.get('selection_eligible') is False or \
                        (review.get('quote_window') or {}).get('status') != 'closed':
                    raise ValueError('This carrier needs a complete, eligible quote '
                                     'and a closed quote round before you can choose it.')
                choice = {'vendor_id': vendor['id'], 'vendor_name': vendor['name'],
                          'operator_id': user_id, 'chosen_at': utcnow(),
                          'source_message_id': action['source_message_id'],
                          'evidence_quote': action['evidence_quote'],
                          'quote_message_id': candidate['message_id'],
                          'quote_id': candidate['quote_id']}
                if (review.get('operator_choice') or {}).get('vendor_id') == vendor['id']:
                    return review['operator_choice']
                updated_review = {
                    **review, 'operator_choice': choice,
                    'automated_selection': review.get('automated_selection') or
                    review.get('selection'),
                    'selection': {**(review.get('selection') or {}),
                                  'outcome': 'selection_proposal',
                                  'selected': candidate,
                                  'basis': 'operator_choice'}}
                updated_state = {**state, 'recovery_quotes': updated_review}
                graph.update_state(config, {
                    'recovery_quotes': updated_review,
                    'fleet_plan': {**(state.get('fleet_plan') or {}),
                                   'load_transfer': load_transfer_status(updated_state)}},
                    as_node='compose_reply')
            with self._db() as db:
                sequence = db.execute('SELECT COALESCE(MAX(sequence), 0) '
                                      'FROM workflow_events WHERE session_id=?',
                                      (session_id,)).fetchone()[0]
                db.execute('INSERT INTO workflow_events VALUES (?,?,?,?,?)',
                           (session_id, sequence + 1, json.dumps(['recovery_quotes']),
                            'operator_carrier_choice', json.dumps(choice)))
            self._refresh_case_projection(session_id)
            return choice

    def _record_recovery_trip(self, session_id):
        """Create the trip once the broker confirms; a failure retries on the next sweep."""
        try:
            return self.create_recovery_shipment(session_id)
        except Exception as error:
            return {'outcome': 'failed', 'error_type': type(error).__name__}

    @observed_service('recovery_dispatch', 'create_recovery_shipment')
    def create_recovery_shipment(self, session_id):
        """Record the load-transfer trip in OpenBoxes once its gates pass."""
        with self.lock:
            session = self._session(session_id)
            if not session:
                raise UnknownSessionError('Unknown session')
            with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
                graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
                config = {'configurable': {'thread_id': session_id}}
                state = graph.get_state(config).values or {}
                if (state.get('shipment') or {}).get('id') != session['shipment_id']:
                    raise ValueError('Shipment identity mismatch in saved case')
                result = create_recovery_trip(self._wms, state)
                if result['outcome'] != 'created':
                    return result
                review = {**(state.get('recovery_quotes') or {}), 'recovery_shipment': {
                    **{key: result[key] for key in (
                        'id', 'shipmentNumber', 'status', 'handoff_state',
                        'idempotency_key', 'accepted_message_id')},
                    'created_by': 'system',
                    'created_at': datetime.now(timezone.utc).isoformat()}}
                updated_state = {**state, 'recovery_quotes': review}
                update = {'recovery_quotes': review,
                          'fleet_plan': {**(state.get('fleet_plan') or {}),
                                         'load_transfer': load_transfer_status(updated_state)}}
                graph.update_state(config, update, as_node='compose_reply')
            with self._db() as db:
                sequence = db.execute('SELECT COALESCE(MAX(sequence), 0) '
                                      'FROM workflow_events WHERE session_id=?',
                                      (session_id,)).fetchone()[0]
                db.execute('INSERT INTO workflow_events VALUES (?,?,?,?,?)',
                           (session_id, sequence + 1, json.dumps(['recovery_dispatch']),
                            'recovery_shipment_created', json.dumps(result)))
            self._refresh_case_projection(session_id)
            return result

    def _incident_session(self, shipment_id):
        with self._db() as db:
            row = db.execute('SELECT id FROM workflow_sessions WHERE shipment_id=? '
                             'ORDER BY created_at DESC LIMIT 1', (shipment_id,)).fetchone()
        return row[0] if row else self.create_session(shipment_id)['session']['id']

    def _incident_state(self, session_id):
        with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
            graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
            return graph.get_state({'configurable': {'thread_id': session_id}}).values or {}

    def arrange_backup_truck(self, shipment_id, *, requested_by, sla=None, wait=True):
        """wait=False (the monitor) skips a shipment another run is already working."""
        with self.backup_truck_locks_guard:
            lock = self.backup_truck_locks.setdefault(shipment_id, threading.Lock())
        if not lock.acquire(blocking=wait):
            return {'outcome': 'in_progress'}
        try:
            return self._arrange_backup_truck(shipment_id, requested_by=requested_by, sla=sla)
        finally:
            lock.release()

    @observed_service('backup_truck', 'arrange_backup_truck')
    def _arrange_backup_truck(self, shipment_id, *, requested_by, sla=None):
        """Advance one backup-truck request from the driver's verified emails.

        Each new driver reply runs through the incident workflow, which asks the backup
        carriers for quotes once a location is known. Facts still missing are asked of
        the driver in one model-written email per reply.
        """
        wms = self._wms()
        try:
            shipment = wms.get_shipment_history(shipment_id)['shipment']
        finally:
            wms.close()
        if shipment.get('synthetic') is not True:
            return {'outcome': 'review', 'reason': 'synthetic_shipment_required'}
        mail = self.mail_factory(self.config.agentmail_key, self.config.agentmail_inbox)
        try:
            outreach, replies = verified_contact_messages(
                mail, self.config.state_path, shipment_id, 'driver')
        finally:
            mail.client.close()
        if not replies:
            return {'outcome': 'no_driver_report',
                    'reason': 'the driver has not reported the incident by email yet'}
        session_id = self._incident_session(shipment_id)
        known = {item.get('id') for item in self._incident_state(session_id).get('messages') or []}
        for reply in replies:
            if reply['message_id'] not in known:
                self.send_message(session_id, reply['text'], source=reply)
        state = self._incident_state(session_id)
        if not state.get('backup_truck_request'):
            with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
                IncidentWorkflow(None, None, checkpointer=checkpointer).graph.update_state(
                    {'configurable': {'thread_id': session_id}},
                    {'backup_truck_request': {'requested_by': requested_by,
                                              'requested_at': utcnow()}},
                    as_node='compose_reply')
        result = {'outcome': 'reviewed', 'session_id': session_id,
                  **backup_truck_outcome(state)}
        if state.get('incident') not in {'breakdown', 'accident'}:
            return {**result, 'outcome': 'incident_unclear'}
        if result['driver_questions']:
            result['driver_email'] = self._ask_driver(
                shipment, state, result, replies[-1]['message_id'], sla)
        return result

    def _ask_driver(self, shipment, state, result, latest_reply_id, sla):
        """One model-written question per driver reply, on the driver's own thread."""
        model = self.model_factory(self.config.openrouter_key, self.config.openrouter_model)
        mail = self.mail_factory(self.config.agentmail_key, self.config.agentmail_inbox)
        drafted = []

        def compose(*args, **kwargs):
            body = model_contact_drafter(model, operator_request=driver_request(
                shipment['shipmentNumber'], result['driver_questions'],
                bool(result['quotes_requested_from'])), sla=sla)(*args, **kwargs)
            drafted.append(body)
            return body
        try:
            sent = send_contact_check(
                mail, self.config.state_path, shipment, state,
                'backup-truck:' + latest_reply_id, 'driver', compose=compose,
                follow_up_hours=self.config.contact_follow_up_hours,
                new_question_allowed=True)
        finally:
            mail.client.close()
            model.client.close()
        if drafted and sent['outcome'] == 'sent':
            sent['email_body'] = drafted[-1]
            self._record_shipment_activity(lambda recorder: recorder.request(
                shipment['id'], 'driver', sent,
                {'why': 'the details recovery carriers need to quote a backup truck',
                 'asked_for': result['driver_questions']}, initiated_by='operator'))
        return sent

    def _save_inspection(self, session_id, change):
        """Apply change(state) -> state update for the inspection loop, under the lock."""
        with self.lock:
            with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
                graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
                config = {'configurable': {'thread_id': session_id}}
                state = graph.get_state(config).values or {}
                update = change(state)
                if update:
                    graph.update_state(config, update, as_node='compose_reply')
            self._refresh_case_projection(session_id)
        return update

    @observed_service('inspection', 'inspector_reply_check')
    def advance_inspection(self, session_id, *, now=None):
        """One turn of the inspection loop: observe the newest reply, decide, act."""
        state = self._incident_state(session_id)
        inspection = (state.get('outreach') or {}).get('inspection') or {}
        review = state.get('inspection_review') or {}
        if review.get('state') in {'cleared', 'not_cleared', 'needs_person'}:
            return {'outcome': review['state']}
        if not self.config.agentmail_key or not self.config.openrouter_key:
            return {'outcome': 'not_configured'}
        mail = self.mail_factory(self.config.agentmail_key, self.config.agentmail_inbox)
        try:
            replies = inspector_replies(mail, {
                **inspection, 'our_message_ids': [item.get('message_id') for item in
                                                  review.get('sent') or []]})
            if not replies or review.get('checked_message_id') == replies[-1]['message_id']:
                # Nothing new from the inspector: act on silence, not on an old reply.
                return self._inspection_silence(session_id, state, review, mail, now)
            latest = replies[-1]
            model = self.model_factory(self.config.openrouter_key,
                                       self.config.openrouter_model)
            assessor = self.inspection_assessor_factory(self.config.openrouter_key)
            try:
                report = extract_inspection_report(model, latest)
                decision = assessor.decide(case_context(state), latest)
                step = next_inspection_step(decision, report)
                action = self._inspection_action(state, step, report, latest, mail, model)
            finally:
                assessor.close()
                model.client.close()
        finally:
            mail.client.close()
        entry = {'message_id': latest['message_id'], 'received_at': latest['timestamp'],
                 'report': {key: value for key, value in report.items()
                            if key != 'model_audit'},
                 'jev': decision, 'step': step, 'action': action}

        def change(current):
            saved = current.get('inspection_review') or {}
            updated = {**saved, 'checked_message_id': latest['message_id'],
                       'history': [*(saved.get('history') or []), entry],
                       'state': action.get('state') or saved.get('state') or 'in_progress'}
            if action.get('sent'):
                updated['sent'] = [*(saved.get('sent') or []), action['sent']]
            if action.get('booking'):
                updated['booking'] = action['booking']
            if action.get('needs_person'):
                updated['needs_person'] = action['needs_person']
            update = {'inspection_review': updated}
            if step == 'record_cleared' and action.get('state') == 'cleared':
                # The inspector's quoted reference is the cargo clearance a transfer needs.
                facts = {**(current.get('facts') or {}), 'inspection_reference': {
                    'value': report['inspection_reference'],
                    'quote': report['reference_quote'],
                    'source_message_id': latest['message_id'],
                    'source_timestamp': latest['timestamp'], 'source': 'inspector'}}
                plan = current.get('fleet_plan') or {}
                transfer = plan.get('load_transfer') or {}
                update.update(facts=facts, fleet_plan={**plan, 'load_transfer': {
                    **transfer, 'missing': [item for item in transfer.get('missing') or []
                                            if item != 'qualified_inspection_reference']}})
            return update
        self._save_inspection(session_id, change)
        self._log_event(session_id, 'inspection_step', entry)
        return {'outcome': 'reviewed', 'step': step, **action}

    def _inspection_silence(self, session_id, state, review, mail, now):
        """Nudge a silent inspector; after the follow-up limit, hand it to a person."""
        now = current_instant(now)
        latest = last_activity(mail, (state.get('outreach') or {}).get('inspection') or {})
        if not silence_due(review, latest, now):
            return {'outcome': 'awaiting_reply', 'state': review.get('state')}
        shipment = state['shipment']
        quiet_since = readable_instant(latest['timestamp'])
        asked = [item for item in review.get('sent') or []
                 if item.get('kind') in {'follow_up', 'nudge'} and
                 item.get('outcome') in {'sent', 'already_sent'}]
        if len(asked) >= FOLLOW_UP_LIMIT:
            person = {'reason': f'the inspector has not replied since {quiet_since}, after '
                                f'{len(asked)} follow-ups', 'message_id': None}
            self._save_inspection(session_id, lambda current: {'inspection_review': {
                **(current.get('inspection_review') or {}), 'state': 'needs_person',
                'needs_person': person}})
            self._record_shipment_activity(lambda recorder: recorder.hand_off(
                shipment['id'], step_key('silent', shipment['id'], latest['message_id']), {
                    'cargo_inspection': 'no reply from the inspection company',
                    'no_reply_since': quiet_since, 'follow_ups_sent': len(asked),
                    'blocks': 'the load transfer, which needs a cleared inspection',
                    'person_must': 'contact the inspection company directly or arrange '
                                   'another inspector'},
                'INSPECTOR_NOT_RESPONDING', 'CONTACT_INSPECTOR_DIRECTLY', None))
            self._log_event(session_id, 'inspection_handed_to_person', person)
            return {'outcome': 'needs_person', **person}
        model = self.model_factory(self.config.openrouter_key, self.config.openrouter_model)
        journal = FleetOutreachJournal(self.config.state_path)
        try:
            sent = reply_on_thread(mail, journal, step_key(
                'nudge', shipment['id'], latest['message_id']), latest,
                write_inspection_nudge(model, state, review, quiet_since))
        except Exception as error:
            sent = {'outcome': 'review', 'reason': 'draft_unavailable',
                    'error_type': type(error).__name__}
        finally:
            journal.close()
            model.client.close()
        nudge = {**sent, 'kind': 'nudge', 'quiet_since': quiet_since}
        self._save_inspection(session_id, lambda current: {'inspection_review': {
            **(current.get('inspection_review') or {}),
            'state': (current.get('inspection_review') or {}).get('state') or 'in_progress',
            'sent': [*((current.get('inspection_review') or {}).get('sent') or []), nudge]}})
        self._log_event(session_id, 'inspection_nudge', nudge)
        return {'outcome': 'nudged' if sent['outcome'] == 'sent' else sent['outcome'],
                'quiet_since': quiet_since}

    def _inspection_action(self, state, step, report, reply, mail, model):
        shipment = state['shipment']
        review = state.get('inspection_review') or {}
        if step == 'record_cleared':
            return {'state': 'cleared'}
        if step == 'record_not_cleared':
            return {'state': 'not_cleared', 'needs_person': {
                'reason': 'the inspector found the load not fit to move',
                'message_id': reply['message_id']}}
        if step == 'propose_booking':
            # Booking sends someone to the roadside: it waits for the operator's approval.
            return {'state': 'booking_proposed', 'booking': {
                'state': 'awaiting_operator_approval',
                'availability_quote': report['availability_quote'],
                'fee_usd': report['fee_usd'], 'fee_quote': report['fee_quote'],
                'message_id': reply['message_id'], 'thread_id': reply['thread_id']}}
        if step == 'follow_up':
            follow_ups = [item for item in review.get('sent') or []
                          if item.get('kind') in {'follow_up', 'nudge'} and
                          item.get('outcome') in {'sent', 'already_sent'}]
            if len(follow_ups) >= FOLLOW_UP_LIMIT:
                return {'state': 'needs_person', 'needs_person': {
                    'reason': 'the inspector has not settled the open points after our '
                              'follow-ups', 'message_id': reply['message_id']}}
            journal = FleetOutreachJournal(self.config.state_path)
            try:
                sent = reply_on_thread(mail, journal, step_key(
                    'follow-up', shipment['id'], reply['message_id']), reply,
                    write_inspection_follow_up(model, state, report))
            except Exception as error:
                sent = {'outcome': 'review', 'reason': 'draft_unavailable',
                        'error_type': type(error).__name__}
            finally:
                journal.close()
            return {'sent': {**sent, 'kind': 'follow_up'}}
        if step == 'hand_to_person':
            return {'state': 'needs_person', 'needs_person': {
                'reason': 'the inspector\'s reply needs a fleet decision',
                'message_id': reply['message_id']}}
        return {}

    def book_inspection(self, shipment_id, approved_by):
        """The operator approved the inspector's offer; confirm it on the thread."""
        session_id = self._incident_session(shipment_id)
        state = self._incident_state(session_id)
        review = state.get('inspection_review') or {}
        booking = review.get('booking') or {}
        if booking.get('state') == 'booked':
            return {'outcome': 'already_booked', 'booking': booking}
        if booking.get('state') != 'awaiting_operator_approval':
            return {'outcome': 'no_offer', 'reason': 'the inspector has not offered a time'}
        decisions = state.get('decisions') or {}
        health = decisions.get('driver_health') or {}
        if health.get('choice') != 'safe' or health.get('confidence', 0) < .8:
            return {'outcome': 'blocked',
                    'reason': 'the driver has not confirmed that everyone is safe'}
        if not ((state.get('facts') or {}).get('roadside_location') or {}).get('value'):
            return {'outcome': 'blocked', 'reason': 'the truck\'s location is not confirmed'}
        mail = self.mail_factory(self.config.agentmail_key, self.config.agentmail_inbox)
        model = self.model_factory(self.config.openrouter_key, self.config.openrouter_model)
        journal = FleetOutreachJournal(self.config.state_path)
        try:
            email = write_booking(model, state, {**booking, 'approved_by': approved_by})
            sent = reply_on_thread(mail, journal, step_key(
                'booking', shipment_id, booking['message_id']),
                {'message_id': booking['message_id'], 'thread_id': booking['thread_id']},
                email)
        except Exception as error:
            sent = {'outcome': 'review', 'reason': 'draft_unavailable',
                    'error_type': type(error).__name__}
        finally:
            journal.close()
            model.client.close()
            mail.client.close()
        if sent['outcome'] not in {'sent', 'already_sent'}:
            return {'outcome': sent['outcome'], 'reason': sent.get('reason')}
        booked = {**booking, 'state': 'booked', 'approved_by': approved_by,
                  'booked_at': utcnow()}

        def change(current):
            saved = current.get('inspection_review') or {}
            return {'inspection_review': {**saved, 'state': 'booked', 'booking': booked,
                                          'sent': [*(saved.get('sent') or []),
                                                   {**sent, 'kind': 'booking'}]}}
        self._save_inspection(session_id, change)
        self._log_event(session_id, 'inspection_booked', booked)
        return {'outcome': 'booked', 'booking': booked, 'email_body': sent.get('body')}

    def _record_dispatch_step(self, session_id, step, result):
        with self.lock:
            with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
                graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
                config = {'configurable': {'thread_id': session_id}}
                state = graph.get_state(config).values or {}
                review = state.get('recovery_quotes') or {}
                dispatch = review.get('dispatch_request') or {}
                updated = {**dispatch, step: result,
                           'travel_released': False,
                           'cargo_transfer_released': False}
                if step == 'carrier_confirmation':
                    updated['state'] = ('awaiting_driver_notice' if result.get('outcome')
                                        in {'sent', 'already_sent'} else
                                        'carrier_contact_pending_review')
                else:
                    updated['state'] = ('awaiting_carrier_confirmation' if result.get(
                        'outcome') in {'sent', 'already_sent'} else
                        'driver_notice_pending_review')
                updated_review = {**review, 'dispatch_request': updated}
                updated_state = {**state, 'recovery_quotes': updated_review}
                graph.update_state(config, {
                    'recovery_quotes': updated_review,
                    'fleet_plan': {**(state.get('fleet_plan') or {}),
                                   'load_transfer': load_transfer_status(updated_state)}},
                    as_node='compose_reply')
            with self._db() as db:
                sequence = db.execute('SELECT COALESCE(MAX(sequence), 0) '
                                      'FROM workflow_events WHERE session_id=?',
                                      (session_id,)).fetchone()[0]
                db.execute('INSERT INTO workflow_events VALUES (?,?,?,?,?)',
                           (session_id, sequence + 1, json.dumps(['recovery_quotes']),
                            'operator_' + step, json.dumps(result)))
            self._refresh_case_projection(session_id)

    def _run_operator_dispatch(self, session_id):
        """Run carrier contact and driver notice as separately persisted steps."""
        if not self.config.agentmail_key:
            carrier = {'outcome': 'not_configured'}
            self._record_dispatch_step(session_id, 'carrier_confirmation', carrier)
            return carrier, {'outcome': 'not_started'}
        with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
            graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
            state = graph.get_state({'configurable': {'thread_id': session_id}}).values or {}
        mail = self.mail_factory(self.config.agentmail_key,
                                 self.config.agentmail_inbox)
        journal = FleetOutreachJournal(self.config.state_path)
        writer = self.model_factory(self.config.openrouter_key, self.config.openrouter_model)
        try:
            try:
                carrier = request_carrier_confirmation(mail, journal, state, writer)
            except ValueError as error:
                carrier = {'outcome': 'blocked', 'reason': str(error)}
            except Exception as error:
                carrier = {'outcome': 'send_uncertain',
                           'error_type': type(error).__name__}
            self._record_dispatch_step(session_id, 'carrier_confirmation', carrier)
            if carrier.get('outcome') not in {'sent', 'already_sent'}:
                return carrier, {'outcome': 'not_started'}
            try:
                driver = notify_driver(mail, journal, state, writer)
            except Exception as error:
                driver = {'outcome': 'send_uncertain',
                          'error_type': type(error).__name__}
            self._record_dispatch_step(session_id, 'driver_notice', driver)
            return carrier, driver
        finally:
            journal.close()
            mail.client.close()
            writer.client.close()

    @observed_service('operator_chat', 'openboxes_chat')
    def resolve_operator_reference(self, user_id, location_id, text):
        store = OperatorChatStore(self.config.state_path)
        history = store.context_history(user_id, location_id)
        selected_context = store.active_context(user_id, location_id)
        model = self.model_factory(self.config.openrouter_key,
                                   self.config.openrouter_model)
        try:
            result, model_call = extract_shipment_entity(
                model, text, history, selected_context)
            if result.get('intent'):
                progress_step('operator_intent_identified', message=
                              f'Identified request: {result["intent"][:180]}')
            return {**result, 'model_call': model_call}
        finally:
            model.client.close()

    @observed_capability('fetch_needs_attention', graph_id='operator_chat',
                         activity='Reading Needs Attention entities from OpenBoxes')
    def operator_attention_items(self):
        wms = self._wms()
        try:
            return wms.request('GET', 'api/dashboard/needsAttentionItems')
        finally:
            wms.close()

    @observed_capability('fetch_delayed_shipments', graph_id='operator_chat',
                         activity='Reading delayed shipments from OpenBoxes')
    def operator_delayed_shipments(self):
        wms = self._wms()
        try:
            return wms.request('GET', 'api/dashboard/needsAttentionItems')
        finally:
            wms.close()

    def delayed_card_context(self, attention):
        """Read each visible card's current case and allowed driver contact."""
        items = (attention or {}).get('delayedShipments') or []
        context = {}
        wms = self._wms()
        try:
            for item in items[:10]:
                shipment_id = item.get('id') if isinstance(item, dict) else None
                if not shipment_id:
                    continue
                case = self.operator_case_record(shipment_id)
                shipment = {}
                try:
                    snapshot = wms.get_shipment_history(shipment_id)['shipment']
                    if snapshot.get('id') == shipment_id:
                        shipment = snapshot
                except Exception:
                    pass
                context[shipment_id] = {'shipment': shipment, 'case': case}
        finally:
            wms.close()
        return context

    def focus_operator_shipment(self, user_id, location_id, shipment_number,
                                shipment_id=None):
        with self.chat_lock:
            store = OperatorChatStore(self.config.state_path)
            return {'messages': self._hydrate_legacy_shipment_cards(
                store, user_id, location_id,
                store.start_context(user_id, location_id, shipment_number,
                                    shipment_id))}

    def console_focus_operator_shipment(self, console_id, shipment_id=None,
                                        shipment_number=None):
        if not shipment_id and shipment_number:
            matches = [item for item in self.list_shipments(shipment_number)
                       if str(item.get('shipment_number') or '').casefold() ==
                       shipment_number.casefold() and item.get('synthetic') is True]
            if len(matches) != 1:
                raise ValueError('Select a synthetic shipment shown in this console')
            shipment_id = matches[0]['id']
        if not shipment_id:
            raise ValueError('Select a synthetic shipment shown in this console')
        wms = self._wms()
        try:
            shipment = wms.get_shipment_history(shipment_id)['shipment']
        finally:
            wms.close()
        if str(shipment.get('id')) != shipment_id or shipment.get('synthetic') is not True:
            raise ValueError('Select a synthetic shipment shown in this console')
        return self.focus_operator_shipment('local-console:' + console_id,
                                            'local-console', shipment.get('shipmentNumber'),
                                            shipment_id)

    @observed_service('operator_chat', 'openboxes_chat')
    def operator_message(self, user_id, username, location_id, text,
                         shipment_context=None, resolution_note=None,
                         request_id=None, include_model_calls=False,
                         intent_type=None, attention_context=None,
                         shipment_search_context=None,
                         excluded_shipment_reference=None,
                         entity_source_message_id=None,
                         contact_role=None, can_write=True,
                         recall_context=None, tow_service_reference=None,
                         data_ticket=None):
        with self.chat_lock:
            store = OperatorChatStore(self.config.state_path)
            if isinstance(shipment_context, dict) and shipment_context.get('id') and \
                    shipment_context.get('shipmentNumber'):
                store.start_context(user_id, location_id,
                                    shipment_context['shipmentNumber'],
                                    shipment_context['id'],
                                    source_message_id=entity_source_message_id)
            history = store.context_history(user_id, location_id)
            model = self.model_factory(self.config.openrouter_key,
                                       self.config.openrouter_model,
                                       capture_trace=include_model_calls)
            selector = (self.selector_factory(self.config.openrouter_key,
                                              capture_trace=include_model_calls)
                        if self.selector_factory and self.config.openrouter_key else None)
            chooser = (self.component_chooser_factory(self.config.openrouter_key,
                                                      capture_trace=include_model_calls)
                       if self.component_chooser_factory and self.config.openrouter_key
                       else None)
            ranker = (self.next_step_factory(self.config.openrouter_key,
                                             capture_trace=include_model_calls)
                      if self.next_step_factory and self.config.openrouter_key else None)
            links = {}
            turn = None
            try:
                case_record = (self.operator_case_record(shipment_context['id'])
                               if isinstance(shipment_context, dict) and
                               shipment_context.get('id') else None)
                sources = ['Saved incident and carrier case'] if case_record else []
                store.update_progress(request_id, user_id, location_id,
                                      sources=sources)
                turn = ChatTurn(
                    service=self, store=store, model=model, text=text, history=history,
                    actor=Actor(user_id, location_id, 'write' if can_write else 'read',
                                request_id),
                    shipment_context=(shipment_context if isinstance(shipment_context, dict)
                                      else None),
                    case_record=case_record, resolution_note=resolution_note,
                    intent_type=intent_type, contact_role=contact_role,
                    tow_service_reference=tow_service_reference,
                    recall_context=recall_context, attention_context=attention_context,
                    shipment_search_context=shipment_search_context,
                    excluded_shipment_reference=excluded_shipment_reference,
                    include_model_calls=include_model_calls,
                    has_selector=selector is not None, component_chooser=chooser,
                    data_ticket=data_ticket)
                registry = self._chat_registry()
                steps = OperatorLoop(registry, selector).run(turn)
                _, cards, _ = compose(steps)
                # Steps return facts and data cards; the language model writes every reply.
                # A step whose own answer a model already wrote is not rewritten.
                answered_by = 'writer'
                written = [step.result.get('model_reply') for step in steps]
                try:
                    if len(steps) == 1 and written[0]:
                        reply = written[0]
                        annotations = steps[0].result.get('annotations') or []
                        answered_by = 'model_step'
                    else:
                        reply, annotations = self._chat_answer(turn, steps)
                    # Records a step read are cited on the reply, whoever wrote it.
                    cited = [source for step in steps for source in
                             step.result.get('sources') or []]
                    if cited:
                        annotations = [*annotations, {'line': 0, 'sources': cited}]
                except Exception:
                    if not cards:
                        raise
                    # The tools' results are complete; do not lose them to a writer outage.
                    answered_by = 'writer_failed'
                    reply = ('I could not write a summary just now because the language '
                             'model did not respond. The results below are complete; '
                             'ask again for a written summary.')
                    annotations = []
                links = self._link_cards(turn, cards)
                items = (findings(getattr(turn, 'highlights', None), cards)
                         if answered_by == 'writer' else [])
                suggestion, actions = self._next_steps(registry, turn, steps, reply, ranker,
                                                       model, items)
                insight = insights_card(reply, items, links, actions)
                cards = [*cards, *(item for item in (insight, suggestion) if item)]
            finally:
                model.client.close()
                if selector:
                    selector.close()
                if chooser:
                    chooser.close()
                if ranker:
                    ranker.close()
            reply = link_reply(public_reply_text(reply, private_record_ids(case_record)), links)
            progress_step('save_operator_reply')
            message_id = store.append_exchange(user_id, location_id, text.strip(), reply,
                                  cards, annotations)
            store.update_progress(request_id, user_id, location_id, 'complete')
            result = {'messages': self._hydrate_legacy_shipment_cards(
                store, user_id, location_id, store.history(user_id, location_id)),
                'recall_notices': RecallChatNoticeStore(
                    self.config.state_path).for_warehouse(location_id)}
            result['user_message_id'] = message_id
            if turn.resolved_attention is not None:
                result['attention_context'] = turn.resolved_attention
            if include_model_calls:
                # One list in call order, so the console can show the run as it happened.
                result['model_calls'] = sorted(
                    [*(getattr(model, 'trace', []) or []),
                     *(getattr(selector, 'trace', []) or []),
                     *(getattr(chooser, 'trace', []) or []),
                     *(getattr(ranker, 'trace', []) or [])],
                    key=lambda call: call.get('started_at') or 0)
                result['loop_steps'] = [step.observation() | {
                    'decided_by': step.decided_by, 'confidence': step.confidence,
                    'started_at': step.started_at, 'finished_at': step.finished_at}
                    for step in steps]
                result['answered_by'] = answered_by
                result['selection_errors'] = turn.selection_errors
            return result

    def _link_cards(self, turn, cards):
        """Links to the OpenBoxes pages of records the cards name; none if lookup fails."""
        if not any(card_labels(card) for card in cards):
            return {}
        try:
            wms = self._wms()
            try:
                return link_cards(cards, lambda labels: wms.entity_ids(
                    labels, self._ops_location(turn), turn.data_ticket))
            finally:
                wms.close()
        except Exception:
            return {}

    @staticmethod
    def _next_steps(registry, turn, steps, reply, ranker, model, items=()):
        """Jev-ranked suggestions and per-finding actions; an outage only loses them."""
        if ranker is None:
            return None, {}
        try:
            progress_step('suggest_next_steps', message='Choosing suggested next steps')
            return suggest(registry, turn, steps, reply, ranker, model, items)
        except Exception as error:
            turn.selection_errors.append('next_steps:' + type(error).__name__)
            return None, {}

    def _supplier_dates_pending(self, location_id):
        journal = SupplierJournal(self.config.state_path)
        try:
            return bool(journal.unapplied_delivery_dates(location_id))
        finally:
            journal.close()

    def _record_shipment_activity(self, record):
        """Write chat contact activity to OpenBoxes; a failure never breaks the chat."""
        try:
            wms = self._wms()
        except Exception:
            return
        try:
            writer = self.model_factory(self.config.openrouter_key,
                                        self.config.openrouter_model)
        except Exception:
            writer = None  # The record is still written, as labelled facts.
        try:
            record(ShipmentActivityRecorder(wms, self.config.state_path, writer))
        except Exception:
            pass
        finally:
            wms.close()
            if writer is not None:
                writer.client.close()

    def _chat_registry(self):
        """Every chat capability with the guardrails it enforces when invoked."""
        tow_denied = 'Write permission is required for tow outreach and vendor changes'

        def has_shipment(turn):
            return bool(turn.shipment_id)

        def has_case(turn):
            return bool(turn.case_record)

        def accident(turn):
            return bool(turn.case_record) and turn.case_record.get('incident') == 'accident'

        def asked(*intents):
            return lambda turn: turn.intent_type in intents

        def quote_asked(action):
            def explicit(turn):
                try:
                    return turn.quote_action()['action'] == action
                except ValueError:
                    return False
            return explicit

        console = lambda turn: turn.actor.location_id == 'local-console'
        return CapabilityRegistry([
            Capability('list_action_items',
                       "Read this warehouse's Needs Attention work queue.",
                       'read', self._chat_action_items, 'Reading Needs Attention items',
                       available=lambda turn: turn.attention_context is not None or
                       console(turn)),
            Capability('list_delayed_shipments',
                       'List delayed, late, or overdue incoming shipments for this warehouse.',
                       'read', self._chat_delayed_shipments, 'Reading delayed shipments',
                       available=lambda turn: turn.shipment_search_context is not None or
                       console(turn)),
            Capability('list_recall_alerts',
                       "List this warehouse's open recall alerts: lots awaiting recall "
                       'review and recorded recall notices. No new FDA search.',
                       'read', self._chat_recall_alerts, 'Reading open recall alerts',
                       available=lambda turn: turn.attention_context is not None or
                       console(turn)),
            Capability('recall_search',
                       'Check current FDA recall sources against the named product and '
                       'its stocked lots.',
                       'read', self._chat_recall, 'Checking product recalls',
                       available=lambda turn: turn.recall_context is not None),
            Capability('recall_notify',
                       'Record a recall notice to other warehouses holding the exact '
                       'affected lot.',
                       'write', self._chat_recall, 'Notifying warehouses about a recall',
                       available=lambda turn: turn.recall_context is not None,
                       explicit=asked('recall_notify')),
            Capability('tow_search',
                       'Search web listings for tow services near the reported accident '
                       'location. Costly; use only when towing is needed.',
                       'read', self._chat_tow, 'Searching for nearby tow services',
                       available=accident, min_confidence=.75),
            Capability('tow_request_quotes',
                       'Email the discovered tow services for conditional quotes.',
                       'outreach', self._chat_tow, 'Requesting tow quotes',
                       available=accident, explicit=asked('tow_request_quotes'),
                       denied_message=tow_denied),
            Capability('tow_check_replies',
                       'Read replies from tow services already asked for quotes.',
                       'read', self._chat_tow, 'Checking tow service replies',
                       available=accident, requires_write=True,
                       explicit=asked('tow_check_replies'), denied_message=tow_denied),
            Capability('tow_save_vendor',
                       'Save one named replying tow service to the vendor database.',
                       'write', self._chat_tow, 'Saving a tow vendor',
                       available=accident, explicit=asked('tow_save_vendor'),
                       denied_message=tow_denied),
            Capability('request_contact_update',
                       "Email the shipment's recorded contact (driver, sending warehouse, "
                       'or shipment company) for a status update on the existing thread. '
                       'Refused while an earlier request awaits a reply.',
                       'contact', self._chat_contact_update,
                       'Preparing a shipment contact status request',
                       available=lambda turn: has_shipment(turn) and
                       bool(self.config.agentmail_key),
                       explicit=asked('driver_check', 'contact_check'),
                       suggest_when=lambda turn: not turn.contact_request_pending(),
                       min_confidence=.8),
            Capability('arrange_backup_truck',
                       'Arrange a backup truck for a broken-down shipment: read the '
                       "driver's emails, ask recovery carriers for conditional quotes once "
                       "the truck's location is known, and ask the driver for anything "
                       'still missing. Books nothing.',
                       'outreach', self._chat_backup_truck, 'Arranging a backup truck',
                       available=lambda turn: has_shipment(turn) and
                       bool(self.config.agentmail_key),
                       explicit=asked('backup_truck'), min_confidence=.8,
                       decides=('It contacts only the configured recovery carriers and the '
                                "shipment's recorded driver, and takes the location, repair "
                                "time, and load from the driver's own emails."),
                       outcome='Emails recovery carriers and the driver'),
            Capability('book_cargo_inspection',
                       'Book the cargo inspection at the time the inspector offered, after '
                       'the operator approves it. Needs confirmed driver safety and location.',
                       'dispatch', self._chat_book_inspection, 'Booking the cargo inspection',
                       available=has_shipment, explicit=asked('book_inspection'),
                       min_confidence=.9,
                       decides=('It books only the time and fee the inspector offered in its '
                                'own email, and only after the driver confirms everyone is '
                                'safe and the location is known.'),
                       outcome='Emails the inspector to confirm the booking'),
            Capability('check_contact_replies',
                       "Read the shipment contact's email thread and quote their latest "
                       'reply. Never sends email.',
                       'read', self._chat_contact_replies,
                       'Checking the saved contact email thread',
                       available=lambda turn: has_shipment(turn) and
                       not turn.contact_requested),
            Capability('query_openboxes_data',
                       "Answer a question about this warehouse's OpenBoxes records by running "
                       'one read-only query: shipments, shipment items, stock by product, lot '
                       'and bin, lots and expiry, bins, and transfers. Use it for lists, '
                       'counts, totals, and filters across records. Never changes data.',
                       'read', self._chat_query_data, 'Querying OpenBoxes data'),
            Capability('review_purchase_orders',
                       'Review open purchase orders into this warehouse: late, due-soon or '
                       'undated deliveries, what is not shipped, open value, which open stock '
                       'requests are waiting on each order, and demand no order covers.',
                       'read', self._chat_purchase_orders, 'Reviewing open purchase orders'),
            Capability('diagnose_open_requests',
                       'Explain why each open stock request is not shipped yet, line by line: '
                       'stock ready to pick, stock still in receiving, backordered on a '
                       'purchase order with its delivery date, or short; and the next step.',
                       'read', self._chat_request_blockers, 'Diagnosing open stock requests'),
            Capability('propose_purchase_orders',
                       'Propose purchase order lines for open-request demand that no stock, '
                       'inbound shipment or open order covers: last supplier and price, '
                       'shortfall plus 30 days of use. Creates nothing.',
                       'read', self._chat_reorder, 'Proposing purchase orders'),
            Capability('create_draft_purchase_orders',
                       'Create the proposed purchase orders as drafts (pending) for a buyer to '
                       'review and place in OpenBoxes. Sends nothing to suppliers.',
                       'write', self._chat_create_purchase_orders, 'Creating draft purchase orders',
                       explicit=asked('create_purchase_order'),
                       denied_message='Write permission is required to create purchase orders',
                       decides=('It drafts orders only for demand nothing else covers, for at most '
                                'three suppliers: each product goes to its last supplier at its last '
                                'price, and the quantity is the shortfall plus 30 days of use, '
                                'rounded up to tens. It takes no supplier, product, or quantity '
                                'from the request.'),
                       outcome='Creates draft purchase orders in OpenBoxes'),
            Capability('review_receiving_bins',
                       'Show lots waiting in receiving bins, how long, whether a putaway task '
                       'covers them, which open requests need them, and a proposed storage '
                       'bin for each. Creates nothing.',
                       'read', self._chat_receiving, 'Reviewing receiving bins'),
            Capability('review_expiry_risk',
                       'Find expired and soon-to-expire lots, value at risk at the current '
                       'issue rate, open requests that can use them first, and expired stock '
                       'still in pickable bins.',
                       'read', self._chat_expiry, 'Reviewing expiry risk'),
            Capability('review_bin_health',
                       'Find bin problems: expired or recall-review lots in pickable bins, '
                       'cold-chain products outside cold storage, stock without a bin or in '
                       'an inactive bin, receiving stock with no putaway, and zone usage.',
                       'read', self._chat_bins, 'Checking bin health'),
            Capability('create_putaway_tasks',
                       'Create one pending putaway task moving receiving-bin stock to the '
                       'proposed storage bins. The team completes it in OpenBoxes.',
                       'write', self._chat_create_putaway, 'Creating a putaway task',
                       explicit=asked('create_putaway'),
                       denied_message='Write permission is required to create warehouse tasks',
                       decides=('It plans every lot waiting in receiving bins itself and takes no '
                                'bin, product, or quantity from the request: cold-chain products '
                                'go to a cold-chain bin and others to a storage bin, a bin already '
                                'holding the product first, else the emptiest. It never puts stock '
                                'into quarantine, hold, staging, or receiving bins, and leaves '
                                'expired lots for a quarantine task.'),
                       outcome='Creates a pending putaway task in OpenBoxes'),
            Capability('quarantine_expired_lots',
                       'Create one pending task moving expired lots from pickable bins to '
                       'the quarantine bin. The team completes it in OpenBoxes.',
                       'write', self._chat_quarantine, 'Creating a quarantine move task',
                       explicit=asked('quarantine_expired'),
                       denied_message='Write permission is required to create warehouse tasks',
                       decides=('It moves every expired lot in a pickable bin to the quarantine '
                                'bin and takes no lot, bin, or quantity from the request.'),
                       outcome='Creates a pending quarantine task in OpenBoxes'),
            Capability('chase_supplier',
                       'Email the supplier of one open purchase order for its ship and '
                       'delivery dates. Refused while an earlier email awaits a reply.',
                       'outreach', self._chat_chase_supplier, 'Emailing the supplier',
                       available=lambda turn: bool(self.config.agentmail_key),
                       explicit=asked('supplier_chase'),
                       denied_message='Write permission is required to email suppliers',
                       decides=('It emails about one purchase order the request names, asking for '
                                'ship and delivery dates; it sends nothing while an earlier email '
                                'awaits a reply.'),
                       outcome='Emails the supplier'),
            Capability('check_supplier_reply',
                       "Read the supplier's replies about one purchase order and the "
                       'delivery date they state, with the exact quote. Never sends email.',
                       'read', self._chat_supplier_reply, 'Reading the supplier reply',
                       available=lambda turn: bool(self.config.agentmail_key)),
            Capability('apply_supplier_delivery_date',
                       "Set the purchase order's expected delivery date to the date the "
                       "supplier's verified reply states, citing that reply.",
                       'write', self._chat_apply_supplier_date,
                       'Updating the purchase order delivery date',
                       explicit=asked('apply_supplier_date'),
                       suggest_when=lambda turn: not turn.shipment_id and
                       self._supplier_dates_pending(turn.actor.location_id),
                       denied_message='Write permission is required to change purchase orders',
                       decides=("It uses only the date in the supplier's verified reply for the "
                                'purchase order the request names, never a date from the request.'),
                       outcome='Updates the order date in OpenBoxes'),
            Capability('investigate_shipment',
                       'Investigate what happened to the shipment and why it is delayed: '
                       'read its OpenBoxes events and comments, the driver incident chat, '
                       'and every email thread that mentions it, then build a cited '
                       'timeline and likely cause. Never sends anything.',
                       'read', self._chat_investigate, 'Investigating the shipment history',
                       available=has_shipment),
            Capability('shipment_status',
                       'Summarise the saved incident, recovery, and quote state for the '
                       'shipment.',
                       'read', self._chat_shipment_status, 'Summarising the shipment case',
                       available=has_case),
            Capability('load_transfer_status',
                       'Report the chosen recovery carrier and load-transfer progress.',
                       'read', self._chat_load_transfer, 'Preparing load-transfer status',
                       available=lambda turn: has_case(turn) and bool(turn.selected_vendor_id)),
            Capability('focused_question',
                       'Answer one specific question about the saved incident case with '
                       'cited evidence.',
                       'read', self._chat_focused, 'Answering from the saved case',
                       available=has_case),
            Capability('check_quotes',
                       'Compare the recovery carrier quotes received so far.',
                       'read', self._chat_quotes, 'Comparing carrier quotes',
                       available=lambda turn: has_case(turn) and bool(turn.vendors)),
            Capability('select_vendor',
                       'Choose the recovery carrier the operator named and start dispatch '
                       'confirmation.',
                       'dispatch', self._chat_select_vendor, 'Recording the carrier choice',
                       available=lambda turn: has_case(turn) and bool(turn.vendors),
                       explicit=quote_asked('select_vendor')),
            Capability('follow_up_vendor',
                       'Send a recovery carrier a follow-up about its quote.',
                       'outreach', self._chat_vendor_followup, 'Following up with carriers',
                       available=lambda turn: has_case(turn) and bool(turn.vendors),
                       explicit=quote_asked('follow_up')),
        ], self.config.state_path)

    # Warehouse operations: purchase orders, stock requests, bins and expiry ------------

    OPS_PARTS = ('stock', 'bins', 'putaways', 'requests', 'purchase_orders', 'issues', 'inbound',
                 'suppliers')

    @staticmethod
    def _ops_location(turn):
        location = turn.actor.location_id
        return location if location and not str(location).startswith('local-console') else None

    def _ops(self, turn, fresh=False):
        """This warehouse's operations facts, read once per turn; a write reads them fresh."""
        if fresh or 'all' not in turn.ops_snapshots:
            wms = self._wms()
            try:
                turn.ops_snapshots['all'] = wms.ops_snapshot(
                    self.OPS_PARTS, self._ops_location(turn), turn.data_ticket)
            finally:
                wms.close()
            turn.add_sources('OpenBoxes warehouse operations data')
        return turn.ops_snapshots['all']

    def _cards(self, turn, *blocks):
        return [card for card in (self._show(turn, item) for item in blocks if item['rows']) if card]

    def _chat_purchase_orders(self, turn):
        review = review_purchase_orders(self._ops(turn))
        orders = review['orders']
        journal = SupplierJournal(self.config.state_path)
        try:
            contact = {order['order_number']: journal.thread(turn.actor.location_id,
                                                             order['order_number'])
                       for order in orders}
        finally:
            journal.close()
        card = table('Open purchase orders', [
            ('order_number', 'Order', 'string', 'order'), ('supplier', 'Supplier', 'string'),
            ('status', 'Status', 'string'), ('eta', 'Expected delivery', 'date'),
            ('risk', 'Risk', 'string'), ('shipping', 'Shipping', 'string'),
            ('open_value', 'Open value', 'number'), ('waiting', 'Requests waiting', 'number')],
            [[order['order_number'], order['supplier'], order['status'], order['eta'],
              order['risk'].replace('_', ' '), order['shipping'], order['open_value'],
              len(order['blocking_requests'])] for order in orders])
        observation = {
            'orders_total': len(orders),
            'orders': [{**{key: order[key] for key in (
                'order_number', 'supplier', 'status', 'ordered', 'eta', 'risk', 'days_late',
                'shipping', 'open_value', 'blocking_requests')},
                'open_lines': [f"{line['open']} x {line['product_code']} {line['product']}"
                               for line in order['lines']],
                'shipments': order['shipments'],
                'supplier_emailed_at': (contact.get(order['order_number']) or {}).get('sent_at')}
                for order in orders[:12]],
            'uncovered_demand': review['uncovered_demand'],
            'available_follow_ups': ('On request: email the supplier of one order for '
                                     'dates, read its reply, and apply a confirmed date.')}
        return {'cards': self._cards(turn, card), 'observation': observation}

    def _chat_request_blockers(self, turn):
        requests = diagnose_requests(self._ops(turn))

        def line_text(line):
            parts = [f"{line['pickable']} pickable"]
            if line['in_receiving']:
                parts.append(f"{line['in_receiving']} still in receiving")
            parts += [f"{order['quantity']} on {order['source']} {order['reference']} due "
                      f"{order['eta'] or 'undated'}" for order in line['on_order']]
            if line['short']:
                parts.append(f"{line['short']} short")
            return f"{line['need']} x {line['product_code']}: " + ', '.join(parts)
        card = table('Open stock requests', [
            ('request', 'Request', 'string', 'request'), ('destination', 'For', 'string'),
            ('status', 'Status', 'string'), ('due', 'Due', 'date'),
            ('blocker', 'Blocker', 'string'), ('next_step', 'Next step', 'string')],
            [[item['request'], item['destination'], item['status'], item['due'],
              item['blocker'].replace('_', ' '), item['next_step']] for item in requests])
        observation = {'requests_total': len(requests),
                       'by_blocker': {state: sum(1 for item in requests if item['blocker'] == state)
                                      for state in ('ready', 'needs_putaway', 'waiting_on_supply',
                                                    'short')},
                       'requests': [{**{key: item[key] for key in (
                           'request', 'type', 'destination', 'status', 'due', 'overdue', 'blocker',
                           'next_step')}, 'lines': [line_text(line) for line in item['lines']]}
                           for item in requests[:15]],
                       'allocation_rule': ('Stock is allocated earliest due date first: pickable '
                                           'stock, then receiving bins, then inbound shipments '
                                           'and purchase orders by expected delivery.')}
        return {'cards': self._cards(turn, card), 'observation': observation}

    def _chat_reorder(self, turn):
        proposal = propose_purchase_orders(self._ops(turn))
        card = table('Proposed purchase order lines', [
            ('supplier', 'Supplier', 'string'), ('product_code', 'Product', 'string', 'product'),
            ('quantity', 'Order', 'number'), ('short', 'Short', 'number'),
            ('cover', '30 days use', 'number'), ('unit_price', 'Last price', 'number'),
            ('requests', 'For requests', 'string')],
            [[order['supplier'], line['product_code'], line['quantity'], line['short'],
              line['cover_30_days'], line['unit_price'], ', '.join(line['requests'])]
             for order in proposal['proposals'] for line in order['lines']] +
            [['no supplier on record', line['product_code'], line['quantity'], line['short'],
              line['cover_30_days'], None, ', '.join(line['requests'])]
             for line in proposal['no_supplier_on_record']])
        return {'cards': self._cards(turn, card), 'observation': {
            'proposals': [{key: order[key] for key in ('supplier', 'lines', 'value')}
                          for order in proposal['proposals']],
            'no_supplier_on_record': proposal['no_supplier_on_record'],
            'quantity_rule': ('Shortfall after stock, inbound shipments and open orders, plus 30 '
                              'days of use at the last 90 days of issues, rounded up to tens.'),
            'available_follow_ups': ('On request: create these as draft purchase orders for a '
                                     'buyer to review; nothing is sent to suppliers.')}}

    def _chat_create_purchase_orders(self, turn):
        proposal = propose_purchase_orders(self._ops(turn, fresh=True))
        if not proposal['proposals']:
            return {'outcome': 'nothing_to_do', 'observation': {
                'result': 'nothing_to_do', 'no_supplier_on_record': proposal['no_supplier_on_record'],
                'reason': 'no uncovered demand has a supplier on record'}}
        created, rejected = [], []
        wms = self._wms()
        try:
            for order in proposal['proposals'][:3]:
                lines = [{'productCode': line['product_code'], 'quantity': line['quantity'],
                          'unitPrice': line['unit_price']} for line in order['lines']]
                key = hashlib.sha256(repr((turn.actor.location_id, order['supplier_id'],
                                           sorted((line['productCode'], line['quantity'])
                                                  for line in lines))).encode()).hexdigest()[:32]
                try:
                    created.append(wms.create_draft_purchase_order(
                        order['supplier_id'], lines, key, self._ops_location(turn), turn.data_ticket))
                except OpsTaskRejected as error:
                    rejected.append({'supplier': order['supplier'], 'rule': str(error)})
                except PermissionError as error:
                    return {'outcome': 'permission_required', 'observation': {
                        'result': 'permission_required', 'reason': str(error)}}
        finally:
            wms.close()
        turn.add_sources('OpenBoxes purchase orders')
        card = table('Draft purchase orders', [
            ('order_number', 'Order', 'string', 'order'), ('supplier', 'Supplier', 'string'),
            ('product_code', 'Product', 'string', 'product'), ('quantity', 'Quantity', 'number')],
            [[item['order_number'], item.get('supplier'), line['product_code'], line['quantity']]
             for item in created for line in item.get('lines') or []])
        return {'outcome': 'created' if created else 'rejected', 'cards': self._cards(turn, card),
                'observation': {'result': 'created' if created else 'rejected',
                                'orders': created, 'rejected': rejected,
                                'no_supplier_on_record': proposal['no_supplier_on_record'],
                                'note': ('Drafts are PENDING: a buyer reviews, approves and places '
                                         'them in OpenBoxes. Nothing was sent to suppliers.')}}

    def _chat_receiving(self, turn):
        review = review_receiving(self._ops(turn))
        lots = review['lots']
        card = table('Stock in receiving bins', [
            ('product_code', 'Product', 'string', 'product'), ('lot', 'Lot', 'string'),
            ('bin', 'Bin', 'string', 'bin'), ('quantity', 'Quantity', 'number'),
            ('waiting_days', 'Days waiting', 'number'), ('on_task', 'On putaway task', 'number'),
            ('proposed_bin', 'Proposed bin', 'string', 'bin')],
            [[lot['product_code'], lot['lot'], lot['bin'], lot['quantity'],
              round(lot['waiting_hours'] / 24, 1) if lot['waiting_hours'] is not None else None,
              lot['on_putaway_task'], lot['proposed_bin'] or lot['proposed_reason']] for lot in lots])
        return {'cards': self._cards(turn, card), 'observation': {
            'lots_in_receiving': len(lots), 'lots': lots[:20],
            'putaway_plan': [move['_label'] for move in review['moves']],
            'bin_rule': ('Cold-chain products go to a cold-chain bin, others to an ambient or '
                         'bulk bin; a bin already holding the product comes first, then the '
                         'emptiest. Expired lots go to quarantine instead.'),
            'available_follow_ups': 'On request: create this plan as one pending putaway task.'}}

    def _chat_expiry(self, turn):
        review = review_expiry(self._ops(turn))
        lots = review['lots']
        card = table('Expired and expiring lots', [
            ('product_code', 'Product', 'string', 'product'), ('lot', 'Lot', 'string'),
            ('bin', 'Bin', 'string', 'bin'), ('expires', 'Expires', 'date'),
            ('quantity', 'Quantity', 'number'), ('value_at_risk', 'Value at risk', 'number'),
            ('action', 'Action', 'string')],
            [[lot['product_code'], lot['lot'], lot['bin'], lot['expires'], lot['quantity'],
              lot['value_at_risk'], lot['action']] for lot in lots])
        return {'cards': self._cards(turn, card), 'observation': {
            'window_days': 90, 'lots': lots[:20], 'lots_total': len(lots),
            'expired_in_pickable_bins': sum(1 for lot in lots if lot['action'].startswith('quarantine now')),
            'value_at_risk': review['value_at_risk'], 'quarantine_bin': review['quarantine_bin'],
            'quarantine_plan': [move['_label'] for move in review['moves']],
            'risk_rule': ('At risk is what the last 90 days of issues and open requests will '
                          'not use before the expiry date.'),
            'available_follow_ups': ('On request: create one pending task moving expired '
                                     'lots to quarantine.')}}

    def _chat_bins(self, turn):
        review = review_bins(self._ops(turn))
        issues = review['issues']
        card = table('Bin issues', [
            ('issue', 'Issue', 'string'), ('product_code', 'Product', 'string', 'product'),
            ('lot', 'Lot', 'string'), ('bin', 'Bin', 'string', 'bin'),
            ('quantity', 'Quantity', 'number'), ('detail', 'Detail', 'string')],
            [[item['issue'], item['product_code'], item['lot'], item['bin'], item['quantity'],
              item['detail']] for item in issues])
        counts = {}
        for item in issues:
            counts[item['issue']] = counts.get(item['issue'], 0) + 1
        return {'cards': self._cards(turn, card), 'observation': {
            'issues_total': len(issues), 'issues_by_kind': counts, 'issues': issues[:25],
            'zones': review['zones']}}

    def _create_moves(self, turn, reason, plan):
        if not plan:
            return {'outcome': 'nothing_to_do', 'observation': {
                'result': 'nothing_to_do', 'reason': 'no lot needs a ' + reason + ' task'}}
        moves = [{key: value for key, value in move.items() if not key.startswith('_')}
                 for move in plan]
        wms = self._wms()
        try:
            result = wms.create_bin_moves(reason, moves, task_key(turn.actor.location_id, reason,
                                                                  moves),
                                          self._ops_location(turn), turn.data_ticket)
        except OpsTaskRejected as error:
            return {'outcome': 'rejected', 'observation': {'result': 'rejected',
                                                           'rule': str(error)}}
        except PermissionError as error:
            return {'outcome': 'permission_required', 'observation': {
                'result': 'permission_required', 'reason': str(error)}}
        finally:
            wms.close()
        turn.add_sources('OpenBoxes putaway task')
        card = table(f"Task {result['order_number']}", [
            ('product_code', 'Product', 'string', 'product'), ('lot_number', 'Lot', 'string'),
            ('quantity', 'Quantity', 'number'), ('from_bin', 'From', 'string', 'bin'),
            ('to_bin', 'To', 'string', 'bin')],
            [[line['product_code'], line['lot_number'], line['quantity'], line['from_bin'],
              line['to_bin']] for line in result.get('lines') or []])
        return {'outcome': result['outcome'], 'cards': self._cards(turn, card), 'observation': {
            'result': result['outcome'], 'task': result['order_number'],
            'lines': result.get('lines') or [move['_label'] for move in plan],
            'note': ('A pending task in OpenBoxes. Stock moves only when the team completes '
                     'it, so on-hand quantities are unchanged until then.')}}

    def _chat_create_putaway(self, turn):
        return self._create_moves(turn, 'putaway', review_receiving(self._ops(turn, fresh=True))['moves'])

    def _chat_quarantine(self, turn):
        review = review_expiry(self._ops(turn, fresh=True))
        if review['moves'] and not review['quarantine_bin']:
            return {'outcome': 'no_quarantine_bin', 'observation': {
                'result': 'no_quarantine_bin', 'reason': 'this warehouse has no hold or quarantine bin'}}
        return self._create_moves(turn, 'quarantine', review['moves'])

    def _chat_order(self, turn, orders):
        """The open purchase order the operator named, matched exactly; else None."""
        number = find_order_reference(turn.model, turn.text, conversation_for_model(turn.history)[0])
        return number, next((order for order in orders
                             if number and order['order_number'].casefold() == number.casefold()), None)

    def _chat_chase_supplier(self, turn):
        orders = review_purchase_orders(self._ops(turn))['orders']
        number, order = self._chat_order(turn, orders)
        if not order:
            return {'outcome': 'order_required', 'observation': {
                'result': 'order_required', 'named': number,
                'open_orders': [item['order_number'] for item in orders[:10]],
                'reason': 'name one open purchase order of this warehouse'}}
        mail = self.mail_factory(self.config.agentmail_key, self.config.agentmail_inbox)
        journal = SupplierJournal(self.config.state_path)
        try:
            result = supplier_email(mail, journal, turn.model, turn.actor.location_id, order,
                                    operator_request=turn.text)
        finally:
            journal.close()
            mail.client.close()
        observation = {'result': result['outcome'], 'order_number': order['order_number'],
                       'supplier': order['supplier'], 'risk': order['risk'], 'eta': order['eta'],
                       **{key: result[key] for key in ('sent_at', 'follow_up', 'delivered_to',
                                                       'follow_up_allowed_at') if key in result}}
        if result['outcome'] == 'sent':
            turn.add_sources('Supplier email')
            observation.update(email_body=result['body'],
                               reply_status='pending; a reply cannot have arrived yet')
        return {'outcome': result['outcome'], 'observation': observation}

    def _chat_supplier_reply(self, turn):
        orders = review_purchase_orders(self._ops(turn))['orders']
        number, order = self._chat_order(turn, orders)
        number = order['order_number'] if order else number
        if not number:
            return {'outcome': 'order_required', 'observation': {
                'result': 'order_required', 'reason': 'name the purchase order to check'}}
        mail = self.mail_factory(self.config.agentmail_key, self.config.agentmail_inbox)
        journal = SupplierJournal(self.config.state_path)
        try:
            found = read_supplier_reply(mail, journal, turn.model, turn.actor.location_id, number)
        except Exception:
            found = {'outcome': 'check_failed'}
        finally:
            journal.close()
            mail.client.close()
        turn.add_sources('Supplier email thread')
        observation = {'result': found['outcome'], 'order_number': number,
                       'current_expected_delivery': order['eta'] if order else None,
                       **{key: found[key] for key in ('summary', 'date_found', 'unverified',
                                                      'short_or_substituted', 'sent_at')
                          if key in found}}
        if found.get('date'):
            observation['stated_date'] = {key: found['date'][key] for key in (
                'kind', 'date', 'phrase', 'quote', 'basis', 'received_at')}
            observation['can_apply'] = found['date']['kind'] == 'delivery'
            if found['date']['kind'] == 'ship':
                observation['note'] = ('The supplier gave a ship date, not a delivery date; '
                                       'the order is not updated from a ship date.')
        return {'outcome': found['outcome'], 'observation': observation}

    def _chat_apply_supplier_date(self, turn):
        orders = review_purchase_orders(self._ops(turn, fresh=True))['orders']
        number, order = self._chat_order(turn, orders)
        if not order:
            return {'outcome': 'order_required', 'observation': {
                'result': 'order_required', 'named': number,
                'reason': 'name one open purchase order of this warehouse'}}
        journal = SupplierJournal(self.config.state_path)
        try:
            stated = journal.latest_date(turn.actor.location_id, order['order_number'])
            if not stated:
                return {'outcome': 'no_verified_date', 'observation': {
                    'result': 'no_verified_date', 'order_number': order['order_number'],
                    'reason': "check the supplier's reply first; no quoted date is saved"}}
            if stated['kind'] != 'delivery':
                return {'outcome': 'ship_date_only', 'observation': {
                    'result': 'ship_date_only', 'order_number': order['order_number'],
                    'quote': stated['quote'],
                    'reason': 'the supplier stated a ship date, not a delivery date'}}
            if stated['applied_at']:
                return {'outcome': 'already_applied', 'observation': {
                    'result': 'already_applied', 'order_number': order['order_number'],
                    'date': stated['date']}}
            wms = self._wms()
            try:
                result = wms.update_po_delivery(
                    order['order_number'], stated['date'],
                    {'messageId': stated['message_id'], 'quote': stated['quote'],
                     'receivedAt': stated['received_at']},
                    self._ops_location(turn), turn.data_ticket)
            except OpsTaskRejected as error:
                return {'outcome': 'rejected', 'observation': {
                    'result': 'rejected', 'order_number': order['order_number'], 'rule': str(error)}}
            except PermissionError as error:
                return {'outcome': 'permission_required', 'observation': {
                    'result': 'permission_required', 'reason': str(error)}}
            finally:
                wms.close()
            journal.applied(turn.actor.location_id, order['order_number'], stated['message_id'],
                            instant_text(current_instant()))
        finally:
            journal.close()
        turn.add_sources('OpenBoxes purchase order')
        return {'outcome': 'updated', 'observation': {
            'result': 'updated', 'order_number': order['order_number'],
            'previous_expected_delivery': order['eta'], 'new_expected_delivery': stated['date'],
            'supplier_quote': stated['quote'], 'phrase': stated['phrase'],
            'basis': stated['basis'], 'lines_updated': len(result.get('lines') or []),
            'note': "The supplier's words are saved as a comment on the order."}}

    def _chat_action_items(self, turn):
        turn.resolved_attention = (turn.attention_context if turn.attention_context
                                   is not None else self.operator_attention_items())
        turn.add_sources('OpenBoxes Needs attention')
        return {'observation': attention_facts(turn.resolved_attention)}

    def _chat_recall_alerts(self, turn):
        turn.resolved_attention = (turn.attention_context if turn.attention_context
                                   is not None else self.operator_attention_items())
        notices = RecallChatNoticeStore(self.config.state_path).for_warehouse(
            turn.actor.location_id)
        turn.add_sources('OpenBoxes recall review')
        return {'observation': recall_alerts_facts(turn.resolved_attention, notices)}

    def _chat_delayed_shipments(self, turn):
        records = (turn.shipment_search_context if turn.shipment_search_context
                   is not None else self.operator_delayed_shipments())
        card_context = self.delayed_card_context(records)
        facts, cards = delayed_shipments_view(
            records, turn.excluded_shipment_reference,
            lambda item: card_context.get(item['id'], {}))
        turn.add_sources('OpenBoxes delayed incoming shipments')
        return {'cards': cards, 'observation': facts}

    def _chat_recall(self, turn):
        if turn.recall_context is None and turn.resolution_note:
            # The product lookup already found why there is no stock to check.
            return {'outcome': 'product_unavailable',
                    'observation': {'result': 'product_unavailable',
                                    'lookup': turn.resolution_note}}
        progress_step('fresh_recall_check', graph_id='operator_chat',
                      message='Checking current FDA sources and OpenBoxes bins')
        context = turn.recall_context
        check = fresh_recall_check(turn.model, context)
        if turn.current != 'recall_notify':
            return {'observation': recall_facts(context, check)}
        if check['status'] != 'verified_match':
            return {'outcome': 'not_verified', 'observation': {
                **recall_facts(context, check),
                'warehouse_notice': 'not sent: no exact stocked-lot match was verified'}}
        match = next((item for item in check['matches'] if
                      (context.get('lotReference') or '') in item['lots']), None)
        if not match:
            return {'outcome': 'lot_required', 'observation': {
                'result': 'lot_required',
                'needed': 'one exact affected lot before notifying other warehouses'}}
        issued = RecallChatNoticeStore(self.config.state_path).notify(
            context=context, match=match, sender_warehouse_id=turn.actor.location_id,
            user_id=turn.actor.user_id)
        return {'outcome': 'sent' if issued else 'no_recipients', 'observation': {
            'warehouse_chat_notices': issued,
            'no_other_warehouse_holds_the_lot': not issued}}

    def _chat_tow(self, turn):
        action = turn.current
        if not turn.shipment_id:
            return {'outcome': 'shipment_required', 'observation': {
                'result': 'shipment_required',
                'needed': 'a selected or named shipment with a recorded accident'}}
        if not turn.case_record or turn.case_record.get('incident') != 'accident':
            return {'outcome': 'accident_not_verified', 'observation': {
                'result': 'no reported accident is recorded for this shipment'}}
        roadside = ((turn.case_record.get('facts') or {}).get(
            'roadside_location') or {}).get('value')
        if not roadside:
            return {'outcome': 'location_required', 'observation': {
                'result': 'location_required',
                'needed': 'a reported roadside location before searching for tow services'}}
        tow_store = TowLeadStore(self.config.state_path)
        context = turn.shipment_context
        if action == 'tow_search':
            progress_step('search_tow_services', graph_id='operator_chat',
                          message='Reading Google Maps tow listings and company pages')
            tow_model = self.model_factory(self.config.openrouter_key,
                                           self.config.tow_research_model,
                                           capture_trace=turn.include_model_calls)
            try:
                leads = google_tow_leads(tow_model, roadside,
                                         browser_path=self.config.browser_path,
                                         cdp_url=self.config.cdp_url)
            finally:
                if getattr(turn.model, 'trace', None) is not None:
                    turn.model.trace.extend(getattr(tow_model, 'trace', []) or [])
                if getattr(tow_model, 'client', None):
                    tow_model.client.close()
            tow_store.save_search(turn.shipment_id, leads)
            return {'cards': tow_cards(tow_store, context) if leads else [],
                    'observation': {
                        'google_maps_tow_leads': len(leads),
                        'searched_for': roadside,
                        'cautions': ['confirm the locality before outreach',
                                     'a shown distance is a webpage driving estimate to '
                                     'the business address, not an agreed route or charge',
                                     'services are not approved vendors'],
                        'quotes_need_an_explicit_ask': True}}
        if action == 'tow_request_quotes':
            if not tow_store.for_shipment(turn.shipment_id):
                return {'outcome': 'search_required', 'observation': {
                    'result': 'search_required',
                    'needed': 'a tow-service search first, to verify contacts and distances'}}
            shipment = self._chat_shipment(turn)
            mail = self.mail_factory(self.config.agentmail_key, self.config.agentmail_inbox)
            try:
                outcomes = send_quote_inquiries(mail, tow_store, shipment, roadside,
                                                turn.model)
            finally:
                mail.client.close()
            return {'outcome': 'sent' if any(item['outcome'] == 'sent' for item in outcomes)
                    else 'not_sent', 'cards': tow_cards(tow_store, context),
                    'observation': {
                        'quote_requests': [{'service': item['name'],
                                            'result': item['outcome']} for item in outcomes],
                        'conditional_requests_only': True, 'tow_truck_dispatched': False}}
        if action == 'tow_check_replies':
            shipment = self._chat_shipment(turn)
            mail = self.mail_factory(self.config.agentmail_key, self.config.agentmail_inbox)
            try:
                outcomes = check_quote_replies(mail, tow_store, turn.shipment_id,
                                               synthetic=shipment.get('synthetic') is True)
            finally:
                mail.client.close()
            return {'cards': tow_cards(tow_store, context),
                    'observation': {
                        'tow_replies': [{'service': item['name'], 'result': item['outcome']}
                                        for item in outcomes],
                        'no_inquiry_sent_yet': not outcomes}}
        lead = tow_store.resolve_reference(turn.shipment_id, turn.tow_service_reference)
        if not lead:
            return {'outcome': 'vendor_not_identified', 'observation': {
                'result': 'vendor_not_identified',
                'needed': 'one responding tow service and its listed address, exactly as '
                          'shown in its card'}}
        saved = tow_store.save_vendor(turn.shipment_id, lead['place_id'], turn.actor.user_id)
        return {'outcome': 'saved' if saved else 'already_saved',
                'cards': tow_cards(tow_store, context),
                'observation': {'tow_service': lead['name'],
                                'result': 'saved' if saved else 'already saved',
                                'status': 'pending fleet vetting; not approved for dispatch'}}

    EMAIL_COLUMNS = [{'name': 'sender', 'label': 'From', 'type': 'string', 'role': 'sender'},
                     {'name': 'direction', 'label': 'Direction', 'type': 'enum',
                      'role': 'direction', 'values': ['in', 'out']},
                     {'name': 'at', 'label': 'Sent', 'type': 'date', 'role': 'time'},
                     {'name': 'subject', 'label': 'Subject', 'type': 'string', 'role': 'subject'},
                     {'name': 'body', 'label': 'Message', 'type': 'string', 'role': 'body'}]

    def _email_block(self, title, messages):
        return block('email_thread', title, self.EMAIL_COLUMNS, [
            [item.get('sender'), item.get('direction'), item.get('at'), item.get('subject'),
             item.get('body')] for item in messages])

    def _show(self, turn, data):
        """The chat card for one tool data block; Jev picks when several components fit."""
        return render_component(data, turn.component_chooser, turn.text)

    def _chat_query_data(self, turn):
        location = (turn.actor.location_id if turn.actor.location_id and
                    not str(turn.actor.location_id).startswith('local-console') else None)
        wms = self._wms()
        try:
            data, observation = answer_question(turn.model, wms, turn.text,
                                                conversation_evidence(turn.history),
                                                location_id=location,
                                                ticket=turn.data_ticket)
        finally:
            wms.close()
        turn.add_sources('OpenBoxes read-only data query')
        card = self._show(turn, data) if data else None
        cards = [card] if card else []
        observation['shown_as'] = card['component'] if card else 'reply text only'
        return {'outcome': observation['result'], 'cards': cards, 'observation': observation}

    def _incident_chat_messages(self, shipment_id):
        """Every driver incident chat message saved for this shipment, oldest first."""
        with self._db() as db:
            session_ids = [row[0] for row in db.execute(
                'SELECT id FROM workflow_sessions WHERE shipment_id=? ORDER BY created_at',
                (shipment_id,))]
        messages = {}
        with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
            graph = IncidentWorkflow(None, None, checkpointer=checkpointer).graph
            for session_id in session_ids:
                state = graph.get_state({'configurable': {'thread_id': session_id}}).values or {}
                for item in state.get('messages') or []:
                    if isinstance(item, dict) and item.get('id'):
                        messages[item['id']] = item
        return sorted(messages.values(), key=lambda item: str(item.get('timestamp') or ''))

    def _known_threads(self, shipment_id):
        """The contact and carrier threads this sidecar opened for the shipment."""
        threads = {}
        outreach = OutreachJournal(self.config.state_path)
        try:
            request = outreach.request(shipment_id)
        finally:
            outreach.close()
        if request and request.get('thread_id'):
            threads[request['thread_id']] = request['role']
        with self._db() as db:
            for (thread_id,) in db.execute('SELECT DISTINCT thread_id FROM '
                                           'workflow_carrier_emails WHERE shipment_id=?',
                                           (shipment_id,)):
                threads.setdefault(thread_id, 'carrier')
        return threads

    def _chat_investigate(self, turn):
        if not turn.shipment_id:
            return {'outcome': 'shipment_required',
                    'observation': {'result': 'shipment_required'}}
        wms = self._wms()
        try:
            history = wms.get_shipment_history(turn.shipment_id)
        finally:
            wms.close()
        shipment = history['shipment']
        if shipment.get('id') != turn.shipment_id:
            raise ValueError('Shipment identity changed during the chat action')
        sources = openboxes_sources(history)
        progress_step('investigate_shipment', CAPABILITY_GRAPH,
                      'Reading the driver incident chat')
        sources += incident_chat_sources(self._incident_chat_messages(turn.shipment_id))
        threads_read, email_error = 0, None
        if self.config.agentmail_key:
            progress_step('investigate_shipment', CAPABILITY_GRAPH,
                          f'Searching the mailbox for {shipment["shipmentNumber"]}')
            mail = self.mail_factory(self.config.agentmail_key, self.config.agentmail_inbox)
            try:
                emails, threads_read = email_sources(mail, shipment['shipmentNumber'],
                                                     self._known_threads(turn.shipment_id))
                sources += emails
            except Exception as error:
                email_error = type(error).__name__
            finally:
                mail.client.close()
        counts = {}
        for item in sources:
            counts[item['kind']] = counts.get(item['kind'], 0) + 1
        observation = {'result': 'investigated', 'sources_read': counts,
                       'email_threads_read': threads_read}
        if email_error:
            observation['email_search'] = f'failed ({email_error}); email was not read'
        cards = []
        try:
            found = investigate(turn.model, shipment, sources, turn.text)
            cards.append(self._show(turn, block('investigation_timeline',
                f'What happened to {shipment["shipmentNumber"]}', [
                    {'name': 'at', 'label': 'When', 'type': 'date', 'role': 'time'},
                    {'name': 'what', 'label': 'What happened', 'type': 'string',
                     'role': 'title'},
                    {'name': 'quote', 'label': 'Evidence', 'type': 'string', 'role': 'detail'},
                    {'name': 'source', 'label': 'Source', 'type': 'string', 'role': 'tag'}],
                [[item['source_at'] or item['at'], item['what'], item['quote'],
                  item['source_kind'].replace('_', ' ')] for item in found['timeline']])))
            # The timeline card carries the cited events; the writer gets only the
            # cause and the points still open, so the reply stays short.
            observation.update({
                'shown_below': f'timeline card with {len(found["timeline"])} cited entries; '
                               'do not retell it',
                'latest_event': {key: found['timeline'][-1][key]
                                 for key in ('at', 'what', 'source_kind')},
                'likely_cause': found['likely_cause'],
                'open_questions': found['open_questions'][:2],
                'conflicts': found['conflicts'][:2],
                'record_gaps': found['record_gaps'][:2]})
        except ValueError:
            observation['result'] = 'no_verifiable_timeline'
        turn.add_sources('Shipment investigation')
        return {'outcome': observation['result'], 'cards': [card for card in cards if card],
                'observation': observation}

    def _chat_shipment(self, turn):
        """Re-read the verified shipment and refuse a changed identity."""
        wms = self._wms()
        try:
            shipment = wms.get_shipment_history(turn.shipment_id)['shipment']
        finally:
            wms.close()
        number = (turn.shipment_context or {}).get('shipmentNumber')
        if shipment.get('id') != turn.shipment_id or (
                number and shipment.get('shipmentNumber') != number):
            raise ValueError('Shipment identity changed during the chat action')
        return shipment

    def _chat_contact_update(self, turn):
        """Facts only; the answer writer puts the outcome into words for the operator."""
        role = turn.contact_update_role()
        if not turn.shipment_id:
            return {'outcome': 'shipment_required',
                    'observation': {'contact_role': role, 'result': 'shipment_required'}}
        shipment = self._chat_shipment(turn)
        role = role or (select_contact(shipment) or {}).get('role')
        if shipment.get('synthetic') is not True:
            result = {'outcome': 'review', 'reason': 'synthetic_shipment_required'}
        elif not role or (select_contact(shipment) or {}).get('role') != role:
            result = {'outcome': 'review', 'reason': 'recorded_contact_unavailable'}
        else:
            drafted = []

            def compose(*args, **kwargs):
                body = model_contact_drafter(turn.model, operator_request=turn.text,
                                             sla=turn.sla)(*args, **kwargs)
                drafted.append(body)
                return body

            mail = self.mail_factory(self.config.agentmail_key, self.config.agentmail_inbox)
            try:
                result = send_contact_check(
                    mail, self.config.state_path, shipment, turn.case_record,
                    turn.actor.request_id or str(uuid.uuid4()), role, compose=compose,
                    follow_up_hours=self.config.contact_follow_up_hours,
                    # Only the operator's own ask may add a question after a reply.
                    new_question_allowed=turn.intent_type in {'driver_check',
                                                              'contact_check'})
            finally:
                mail.client.close()
            if drafted and result['outcome'] == 'sent':
                result['email_body'] = drafted[-1]
        outcome = result['outcome']
        observation = {'contact_role': role, 'result': outcome}
        if outcome == 'sent':
            turn.contact_requested = role
            observation.update(delivered_to='the configured test inbox',
                               sent_at=instant_text(current_instant()),
                               email_body=result.get('email_body'),
                               reply_status='pending; a reply cannot have arrived yet')
            self._record_shipment_activity(lambda recorder: recorder.request(
                turn.shipment_id, role, result,
                {'why': "a warehouse user's request in chat"}, initiated_by='operator'))
        elif outcome == 'contact_already_replied':
            message = result['latest_reply']
            observation['latest_reply'] = {key: message.get(key) for key in (
                'message_id', 'timestamp', 'text')}
            observation['not_sent_because'] = (
                result.get('not_needed_reason') or 'this reply answers our latest request')
            turn.add_sources('Verified contact email')
            self._record_shipment_activity(lambda recorder: recorder.replies(
                self.config.state_path, turn.shipment_id))
        elif outcome == 'awaiting_reply':
            observation.update(sent_at=result['sent_at'], retry_after=result['retry_after'],
                               not_sent_because='an earlier request is still awaiting a '
                                                'reply inside the follow-up window')
            return {'outcome': outcome, 'observation': observation, 'final': True}
        elif outcome != 'already_sent':
            observation['reason'] = result.get('reason')
        cards = []
        if outcome == 'sent' and observation.get('email_body'):
            cards.append(self._show(turn, self._email_block(
                f'Email sent to the {CONTACT_LABELS.get(role, "contact")}', [{
                    'sender': 'our_warehouse_team', 'direction': 'out',
                    'at': observation['sent_at'],
                    'subject': f'Shipment status update: {turn.shipment_number}',
                    'body': observation['email_body']}])))
            # The sent email ends the turn, so no timeline or other step follows it;
            # the email card shows right under the reply, then the suggested next steps.
            return {'outcome': outcome, 'cards': [card for card in cards if card],
                    'observation': observation, 'final': True}
        return {'outcome': outcome, 'cards': [card for card in cards if card],
                'observation': observation}

    def _chat_book_inspection(self, turn):
        result = self.book_inspection(turn.shipment_id, turn.actor.user_id)
        cards = []
        if result['outcome'] == 'booked' and result.get('email_body'):
            cards.append(self._show(turn, self._email_block('Email sent to the inspector', [{
                'sender': 'our_warehouse_team', 'direction': 'out', 'at': utcnow(),
                'subject': f'Inspection booking: {turn.shipment_number}',
                'body': result['email_body']}])))
        booking = result.get('booking') or {}
        return {'outcome': result['outcome'], 'final': True,
                'cards': [card for card in cards if card],
                'observation': {'result': result['outcome'], 'reason': result.get('reason'),
                                'inspector_offered': booking.get('availability_quote'),
                                'fee_usd': booking.get('fee_usd'),
                                'next': 'the inspector\'s result and reference unlock the '
                                        'load transfer' if result['outcome'] == 'booked'
                                        else None}}

    def _chat_backup_truck(self, turn):
        if not turn.shipment_id:
            return {'outcome': 'shipment_required',
                    'observation': {'result': 'shipment_required'}}
        progress_step('arrange_backup_truck', 'backup_truck',
                      f'Reading the driver emails for {turn.shipment_number}')
        result = self.arrange_backup_truck(turn.shipment_id, requested_by=turn.actor.user_id,
                                           sla=turn.sla)
        if result['outcome'] not in {'reviewed'}:
            # Nothing to act on yet; the writer explains it from the observation.
            return {'outcome': result['outcome'], 'observation': result}
        turn.case_record = self.operator_case_record(turn.shipment_id)
        names = {item['id']: item['name'] for item in SHIPPERS}
        sent = result.get('driver_email') or {}
        # Facts only: the answer writer replies to what the operator actually said,
        # using the driver's own words from the saved emails.
        observation = {
            'shipment_number': turn.shipment_number,
            'incident': result['incident'],
            'backup_truck_needed': result['transfer_needed'],
            'repair_estimate_hours': result['repair_eta_hours'],
            'pickup_point_for_carriers': result['roadside_location'],
            'quote_requests_sent_to': [names.get(item, item)
                                       for item in result['quotes_requested_from']],
            'quote_cutoff': result['quote_cutoff'],
            'why_quotes_are_on_hold': (
                None if result['quotes_requested_from'] else
                'recovery carriers need a pickup point they can drive to: the road with '
                'its direction and the nearest exit, mile marker, or town. A road name '
                'alone, however long the road, is not enough.'
                if result['transfer_needed'] else
                'no repair time long enough to need a backup truck has been reported'),
            'still_needed_from_driver': result['driver_questions'],
            'cargo_inspection': (
                'the inspection company has cleared the load'
                if result['inspection_stage'] == 'cleared' else
                'an inspection request went to the inspection company; its reply drives the '
                'next step' if result['inspection_requested'] else
                'requested as soon as the truck\'s location is known'),
            'why_the_driver_is_asked_about_the_load': (
                'their report decides whether a transfer is possible and whether cleanup '
                'comes first; a qualified inspection still confirms the load before it '
                'is moved'),
            'driver_email': ({'result': 'sent now', 'asked_for': result['driver_questions']}
                             if sent.get('outcome') == 'sent' else
                             {'result': 'already asked about the driver\'s latest reply; '
                                        'waiting for an answer'}
                             if sent.get('outcome') in {'already_sent', 'awaiting_reply'} else
                             {'result': 'not needed'} if not sent else
                             {'result': 'needs review before retrying'}),
            'booked': 'nothing: no truck is booked and no cargo moves until fleet release',
        }
        cards = []
        if sent.get('outcome') == 'sent':
            cards.append(self._show(turn, self._email_block('Email sent to the driver', [{
                'sender': 'our_warehouse_team', 'direction': 'out', 'at': utcnow(),
                'subject': f'Shipment status update: {turn.shipment_number}',
                'body': sent['email_body']}])))
        turn.add_sources('Driver emails')
        return {'outcome': 'arranged', 'final': True, 'observation': observation,
                'cards': [card for card in cards if card]}

    def _chat_contact_replies(self, turn):
        if not turn.shipment_id:
            return {'outcome': 'shipment_required', 'observation': {
                'result': 'shipment_required',
                'reason': 'which shipment email thread to check could not be verified'}}
        if not self.config.agentmail_key:
            return {'outcome': 'not_configured', 'observation': {
                'result': 'not_configured',
                'reason': 'the email inbox is not configured, so a reply cannot be verified'}}
        progress_step('read_contact_email', graph_id='operator_chat',
                      message='Reading the saved shipment contact thread')
        mail = self.mail_factory(self.config.agentmail_key, self.config.agentmail_inbox)
        thread = []
        try:
            try:
                found = check_contact_replies(mail, self.config.state_path,
                                              turn.shipment_id,
                                              turn.contact_role or None)
                thread = found.get('thread_view') or []
            except Exception:
                found = {'outcome': 'check_failed'}
        finally:
            mail.client.close()
        facts = (contact_reply_facts(found) if found['outcome'] != 'check_failed' else
                 {'result': 'check_failed', 'reply_status': 'unknown'})
        message = found.get('latest_reply') or found.get('previous_reply')
        if message:
            self._record_shipment_activity(lambda recorder: recorder.replies(
                self.config.state_path, turn.shipment_id))
            turn.add_sources('Verified contact email')
        cards = ([self._show(turn, self._email_block(
            f'{CONTACT_LABELS.get(found.get("contact_role"), "Contact").capitalize()} '
            f'email thread · {turn.shipment_number}', thread))] if thread else [])
        return {'cards': [card for card in cards if card], 'observation': facts}

    def _chat_shipment_status(self, turn):
        progress_step('compose_shipment_status', message=
                      f'Summarizing the saved incident and recovery state for '
                      f'{turn.shipment_number}')
        facts, cards = status_view(turn.shipment_context, turn.case_record)
        if turn.actor.can_write and turn.vendors and \
                not carrier_vehicle_dispatched(turn.case_record):
            facts['carrier_inbox_check'] = self._schedule_quote_refresh(turn.session_id)
        return {'cards': cards, 'observation': facts}

    def _chat_load_transfer(self, turn):
        report_refresh = None
        if turn.actor.can_write and turn.session_id:
            try:
                report_refresh = self.refresh_selected_carrier_report(turn.session_id)
                turn.case_record = self.operator_case_record(turn.shipment_id)
            except Exception:
                report_refresh = {'outcome': 'check_failed'}
        progress_step('compose_load_transfer_status', message=
                      f'Preparing the load-transfer status for {turn.shipment_number}')
        facts = load_transfer_facts(turn.shipment_context, turn.case_record)
        facts['latest_carrier_email_checked'] = not (
            report_refresh and report_refresh['outcome'] == 'check_failed')
        return {'observation': facts,
                'sources': load_transfer_sources(turn.case_record)}

    def _chat_focused(self, turn):
        # The language model writes this answer from the case; it is not a template.
        reply, annotations, _ = focused_reply(turn.model, turn.text, turn.shipment_context,
                                              turn.case_record, turn.history)
        return {'model_reply': reply, 'annotations': annotations,
                'observation': {'answer_from_case': reply}}

    def _chat_quotes(self, turn):
        progress_step('summarize_recovery_quotes', message=
                      f'Comparing saved carrier replies for {turn.shipment_number}')
        facts = quote_facts(turn.shipment_context, turn.case_record.get('outreach') or {},
                            turn.case_record.get('recovery_quotes') or {})
        facts['carrier_inbox_check'] = (
            None if not turn.actor.can_write or carrier_vehicle_dispatched(turn.case_record)
            else self._schedule_quote_refresh(turn.session_id))
        return {'cards': carrier_cards(turn.case_record), 'observation': facts}

    def _chat_select_vendor(self, turn):
        action = turn.quote_action()
        vendor = next((item for item in turn.vendors if item['id'] ==
                       action['vendor_id']), None)
        choice = self._record_operator_choice(turn.session_id, vendor,
                                              turn.actor.user_id, action)
        carrier, driver = self._run_operator_dispatch(turn.session_id)
        carrier_outcome = carrier.get('outcome')
        driver_outcome = driver.get('outcome')
        return {'outcome': carrier_outcome or 'review',
                'cards': carrier_cards(self.operator_case_record(turn.shipment_id)),
                'observation': {
                    'chosen_carrier': choice['vendor_name'],
                    'carrier_confirmation_email': carrier_outcome,
                    'carrier_asked_to': 'accept the quote and confirm its pickup timing',
                    'driver_update_email': driver_outcome,
                    'on_hold_until': 'qualified inspection, scene clearance, and fleet '
                                     'release'}}

    def _chat_vendor_followup(self, turn):
        action = turn.quote_action()
        _, case = self._operator_quote_case(turn.shipment_id)
        vendor_id = action['vendor_id']
        targets = turn.vendors if vendor_id == 'all' else [
            vendor for vendor in turn.vendors if vendor['id'] == vendor_id]
        if not case:
            return {'outcome': 'no_quote_round', 'observation': {
                'result': 'no active recovery quote round is recorded for this shipment'}}
        if (case.get('recovery_quotes') or {}).get('operator_choice') and vendor_id == 'all':
            return {'outcome': 'carrier_already_chosen', 'observation': {
                'result': 'not sent: a recovery carrier is already chosen, so no new '
                          'all-carrier request went out',
                'chosen_carrier': case['recovery_quotes']['operator_choice'].get(
                    'vendor_name')}}
        if not targets:
            return {'outcome': 'vendor_required', 'observation': {
                'result': 'vendor_required',
                'carriers_in_this_round': [vendor['name'] for vendor in turn.vendors]}}
        mail = self.mail_factory(self.config.agentmail_key, self.config.agentmail_inbox)
        journal = FleetOutreachJournal(self.config.state_path)
        ledger = ActionLedger(self.config.state_path)
        results = []
        try:
            for vendor in targets:
                try:
                    result = send_vendor_followup(
                        mail, journal, case['shipment'], case.get('outreach') or {},
                        case.get('recovery_quotes') or {}, vendor, topic=action['topic'],
                        new_thread=action['start_new_thread'],
                        operator_id=turn.actor.user_id, operator_text=turn.text,
                        writer=turn.model, ledger=ledger,
                        follow_up_hours=self.config.carrier_follow_up_hours)
                    if result['outcome'] != 'awaiting_reply':
                        self._record_operator_followups(turn.session_id, [result])
                except Exception as error:
                    result = {'outcome': 'error', 'error_type': type(error).__name__}
                results.append(result)
        finally:
            ledger.close()
            journal.close()
            mail.client.close()
        follow_ups = [{
            'carrier': vendor['name'], 'result': result['outcome'],
            'channel': result.get('channel'), 'method': result.get('method'),
            'sent_at': result.get('sent_at'), 'retry_after': result.get('retry_after')}
            for vendor, result in zip(targets, results)]
        return {'outcome': 'sent' if any(item['outcome'] == 'sent' for item in results)
                else results[0]['outcome'],
                'observation': {'follow_ups': follow_ups, 'topic': action['topic'],
                                'updated_quotes_need_fleet_review': True,
                                'vehicle_dispatched': False}}

    def _chat_answer(self, turn, steps):
        """Written answer when no capability produced a direct reply."""
        progress_step('answer_operator', message=
                      f'Drafting an answer using the available case for '
                      f'{turn.shipment_number or "your request"}')
        saved_emails, email_reader = [], None
        shipment_id = turn.shipment_id
        if shipment_id:
            reply_store = ContactReplyStore(self.config.state_path)
            try:
                saved_emails = reply_store.for_shipment(shipment_id)
            finally:
                reply_store.close()
            if self.config.agentmail_key:
                def email_reader():
                    mail = self.mail_factory(self.config.agentmail_key,
                                             self.config.agentmail_inbox)
                    try:
                        thread = read_contact_thread(mail, self.config.state_path,
                                                     shipment_id)
                    finally:
                        mail.client.close()
                    turn.add_sources('Shipment contact email thread')
                    if thread.get('outcome') == 'read':
                        self._record_shipment_activity(lambda recorder: recorder.replies(
                            self.config.state_path, shipment_id))
                    return thread
        chat_graph = OperatorChatGraph(turn.model, email_reader)
        reply, _ = chat_graph.respond(
            turn.actor.user_id, None, turn.actor.location_id, turn.text, turn.history,
            turn.shipment_context, turn.resolution_note, case_for_model(turn.case_record),
            saved_emails, tool_results=[step.observation() for step in steps],
            sla=turn.sla)
        # The reply's bullets as structured findings, for the chat's findings card.
        turn.highlights = getattr(chat_graph, 'highlights', None) or []
        return reply, chat_graph.annotations

    def chat_tool_catalog(self):
        """Every stage and capability the operator chat loop can run, for the console."""
        registry = self._chat_registry()
        tools = [{'id': item.name, 'label': item.name.replace('_', ' ').capitalize(),
                  'description': item.description, 'effect': item.effect,
                  'requires_write': item.requires_write,
                  'min_confidence': item.min_confidence,
                  'needs_explicit_request': item.effect in {'outreach', 'write', 'dispatch'}}
                 for item in registry.capabilities.values()]
        return {'stages': [
            {'id': 'extract_shipment_entity', 'label': 'Request reader',
             'description': 'DeepSeek reads the message, grounds the shipment, product, or '
                            'contact it names, and suggests a capability.'},
            {'id': 'choose_next_capability', 'label': 'Jev chooser',
             'description': 'Jev picks the next capability or chooses to answer.'},
            {'id': 'answer_operator', 'label': 'Answer writer',
             'description': 'DeepSeek writes the reply when no capability answered directly; '
                            'it may read the contact email thread once.'}],
            'tools': tools,
            'limits': {'max_steps': MAX_STEPS, 'max_effects': MAX_EFFECTS}}

    @observed_capability('fetch_recall_context', graph_id='operator_chat',
                         activity='Reading the product and stocked lots from OpenBoxes')
    def _console_recall_context(self, entity):
        """The console's recall evidence; OpenBoxes only serves synthetic test products."""
        product = entity.get('product_reference')
        if not product:
            return None, 'Name one product by its exact code, name, or NDC to check recalls.'
        wms = self._wms()
        try:
            found = wms.get_recall_context(product, entity.get('lot_reference') or None)
        except Exception:
            return None, 'OpenBoxes could not return the product stock for this recall check.'
        finally:
            wms.close()
        outcome = (found or {}).get('outcome')
        if outcome == 'found':
            return found['recall_context'], None
        return None, {
            'synthetic_product_required': 'The console checks recalls only for synthetic '
                                          'test products (codes starting RECALL-TEST-). Use '
                                          'the OpenBoxes chat for other products.',
            'ambiguous_product': 'More than one product matches that name. Use its exact '
                                 'product code or NDC.',
        }.get(outcome, 'No product matches that exact code, name, or NDC.')

    def console_operator_message(self, console_id, text, request_id=None, access='write'):
        """Local operator chat, with the same case and work-queue answer paths."""
        user_id = 'local-console:' + console_id
        location_id = 'local-console'
        with observe_run(self.config.state_path, 'operator_chat',
                         'local_console_chat', 'local synthetic console'):
            entity = self.resolve_operator_reference(user_id, location_id, text)
            reference = entity.get('shipment_reference')
            shipment_context = None
            resolution_note = None
            recall_context = None
            if entity.get('intent_type') in WAREHOUSE_INTENTS:
                pass
            elif entity.get('intent_type') == 'delayed_shipments':
                pass
            elif entity.get('intent_type') in {'recall_search', 'recall_notify'}:
                recall_context, resolution_note = self._console_recall_context(entity)
            elif reference:
                progress_step('openboxes_shipment_lookup', 'openboxes',
                              f'Looking up {reference} in OpenBoxes')
                matches = [item for item in self.list_shipments(reference)
                           if reference.casefold() in {
                               str(item['id']).casefold(),
                               str(item.get('shipment_number') or '').casefold()}]
                if len(matches) == 1 and matches[0].get('synthetic') is True:
                    wms = self._wms()
                    try:
                        shipment = wms.get_shipment_history(matches[0]['id'])['shipment']
                    finally:
                        wms.close()
                    if shipment.get('id') == matches[0]['id'] and \
                            shipment.get('synthetic') is True:
                        shipment_context = {key: shipment.get(key) for key in (
                            'id', 'shipmentNumber', 'status', 'expectedDeliveryDate',
                            'origin', 'destination', 'items', 'humanReviewState')}
                if shipment_context is None:
                    resolution_note = ('That shipment could not be verified as a '
                                       'synthetic OpenBoxes case in the local console.')
            else:
                resolution_note = 'Name a synthetic shipment to inspect its case.'
            result = self.operator_message(user_id, 'Local console', location_id,
                                           text, shipment_context, resolution_note,
                                           request_id, include_model_calls=True,
                                           can_write=access == 'write',
                                           recall_context=recall_context,
                                           intent_type=entity.get('intent_type'),
                                           entity_source_message_id=entity.get('source_message_id'),
                                           contact_role=entity.get('contact_role'),
                                           tow_service_reference=entity.get('tow_service_reference'),
                                           excluded_shipment_reference=(reference if
                                               entity.get('intent_type') == 'delayed_shipments'
                                               else None))
            case = (self.operator_case_record(shipment_context['id'])
                    if shipment_context else None)
            store = OperatorChatStore(self.config.state_path)
            store.update_progress(request_id, user_id, location_id, 'complete')
            model_calls = [{'node': 'extract_shipment_entity', **entity['model_call']},
                           *[{'node': 'choose_next_capability'
                              if item.get('provider') == 'OpenRouter Jev'
                              else 'operator_model_call', **item}
                             for item in result.get('model_calls') or []]]
            trace = {'text': text, 'model_calls': model_calls,
                     'loop_steps': result.get('loop_steps') or [],
                     'answered_by': result.get('answered_by'),
                     'selection_errors': result.get('selection_errors') or []}
            # Saved per message so the console can show any message's run order later.
            store.save_trace(user_id, location_id, result['user_message_id'], trace)
            return {**result, 'shipment_context': shipment_context,
                    'workflow_session_id': (case or {}).get('session_id'),
                    'execution_run_id': (active_observer().run_id
                                         if active_observer() else None),
                    'model_calls': model_calls}


def create_app(service=None):
    automatic_quote_monitor = service is None
    if service is None:
        service = WorkflowService(local_config(),
                                  selector_factory=JevCapabilitySelector,
                                  component_chooser_factory=JevComponentChooser,
                                  next_step_factory=JevNextStepRanker)
    monitor = {'enabled': automatic_quote_monitor, 'running': False,
               'interval_seconds': QUOTE_MONITOR_INTERVAL_SECONDS,
               'last_check_at': None, 'last_started': None,
               'last_due': None, 'last_closed': None,
               'last_error_count': None}

    contact_monitor = {'enabled': automatic_quote_monitor and
                       service.config.contact_auto_chase,
                       'running': False,
                       'interval_minutes': service.config.contact_monitor_interval_minutes,
                       'follow_up_hours': service.config.contact_follow_up_hours,
                       'follow_up_limit': service.config.contact_auto_follow_up_limit,
                       'last_run': None}

    async def monitor_contact_follow_ups():
        while True:
            try:
                contact_monitor['last_run'] = await asyncio.to_thread(
                    ContactFollowUpMonitor(service).run)
            except Exception as error:
                contact_monitor['last_run'] = {'checked_at': utcnow(),
                                               'error_type': type(error).__name__}
            await asyncio.sleep(service.config.contact_monitor_interval_minutes * 60)

    async def monitor_quote_cutoffs():
        while True:
            try:
                result = await asyncio.to_thread(service.sweep_due_recovery_quotes)
                monitor.update(last_check_at=result['checked_at'],
                               last_started=result['started'],
                               last_due=result['due'], last_closed=result['closed'],
                               last_error_count=len(result['errors']))
            except Exception as error:
                monitor.update(last_check_at=utcnow(), last_error_count=1,
                               last_error_type=type(error).__name__)
            await asyncio.sleep(QUOTE_MONITOR_INTERVAL_SECONDS)

    @asynccontextmanager
    async def lifespan(_api):
        tasks = []
        if automatic_quote_monitor:
            monitor['running'] = True
            tasks.append(asyncio.create_task(monitor_quote_cutoffs()))
        if contact_monitor['enabled']:
            contact_monitor['running'] = True
            tasks.append(asyncio.create_task(monitor_contact_follow_ups()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            monitor['running'] = contact_monitor['running'] = False

    api = FastAPI(title='OpenBoxes Incident Workflow', lifespan=lifespan)
    api.state.workflow_service = service
    api.state.quote_monitor = monitor
    api.state.contact_monitor = contact_monitor

    @api.middleware('http')
    async def local_console_or_authenticated_proxy(request: Request, call_next):
        host = request.client.host if request.client else ''
        if host not in {'127.0.0.1', '::1', 'testclient'}:
            token = service.config.openboxes_sidecar_token
            given = request.headers.get('X-OpenBoxes-Sidecar-Token') or ''
            if not request.url.path.startswith('/api/operator-chat') or not token or \
                    not hmac.compare_digest(token, given):
                return JSONResponse({'detail': 'Local console only'}, status_code=403)
        return await call_next(request)

    def operator_identity(request: Request):
        expected = service.config.openboxes_sidecar_token
        given = request.headers.get('X-OpenBoxes-Sidecar-Token') or ''
        if not expected:
            raise HTTPException(503, detail='OpenBoxes chat proxy is not configured')
        if not hmac.compare_digest(expected, given):
            raise HTTPException(403, detail='OpenBoxes proxy authentication required')
        user_id = (request.headers.get('X-OpenBoxes-User-Id') or '').strip()
        username = (request.headers.get('X-OpenBoxes-Username') or '').strip()
        location_id = (request.headers.get('X-OpenBoxes-Location-Id') or '').strip()
        if not user_id or len(user_id) > 100 or not username or len(username) > 100 or \
                len(location_id) > 100:
            raise HTTPException(400, detail='Invalid OpenBoxes user context')
        if request.headers.get('X-OpenBoxes-Chat-Access') not in {'read', 'write'}:
            raise HTTPException(403, detail='Chat permission required')
        return user_id, username, location_id

    def operator_can_write(request: Request):
        return request.headers.get('X-OpenBoxes-Chat-Access') == 'write'

    def console_identity(request: Request, console_id: str):
        if not request.client or request.client.host not in {
                '127.0.0.1', '::1', 'testclient'}:
            raise HTTPException(403, detail='Local console only')
        try:
            canonical = str(uuid.UUID(console_id))
        except ValueError as error:
            raise HTTPException(400, detail='Invalid console ID') from error
        if canonical != console_id:
            raise HTTPException(400, detail='Invalid console ID')
        return 'local-console:' + console_id, 'local-console'

    async def stream_operator_progress(websocket: WebSocket, request_id: str,
                                       user_id: str, location_id: str,
                                       subprotocol: str | None = None):
        subscriber = progress_hub.subscribe(request_id, user_id, location_id)
        store = OperatorChatStore(service.config.state_path)
        try:
            await websocket.accept(subprotocol=subprotocol)
            current = store.progress(request_id, user_id, location_id)
            if current:
                await websocket.send_json(current)
                if current['stage'] in {'complete', 'failed'}:
                    return
            while True:
                update_task = asyncio.create_task(subscriber[1].get())
                receive_task = asyncio.create_task(websocket.receive())
                done, pending = await asyncio.wait(
                    {update_task, receive_task}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if update_task in done:
                    update = update_task.result()
                    await websocket.send_json(update)
                    if update['stage'] in {'complete', 'failed'}:
                        return
                if receive_task in done and \
                        receive_task.result().get('type') == 'websocket.disconnect':
                    return
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            progress_hub.unsubscribe(request_id, user_id, location_id, subscriber)

    @api.get('/')
    def index():
        return FileResponse(HERE / 'workflow_ui' / 'index.html')

    @api.get('/workflow.css')
    def css():
        return FileResponse(HERE / 'workflow_ui' / 'workflow.css', media_type='text/css')

    @api.get('/workflow.js')
    def script():
        return FileResponse(HERE / 'workflow_ui' / 'workflow.js', media_type='text/javascript')

    @api.get('/chat-markdown.js')
    def chat_markdown_script():
        return FileResponse(HERE / 'workflow_ui' / 'chat-markdown.js',
                            media_type='text/javascript')

    @api.get('/api/health')
    def health():
        return {'status': 'ok', 'openrouter_configured': bool(service.config.openrouter_key),
                'openboxes_url': service.config.openboxes_url,
                'quote_monitor': monitor, 'contact_monitor': contact_monitor}

    @api.get('/api/shipments')
    def shipments(q: str = ''):
        try:
            return {'shipments': service.list_shipments(q)}
        except Exception as error:
            raise HTTPException(502, detail='OpenBoxes read failed: ' + type(error).__name__) from error

    @api.get('/api/graphs')
    def graphs():
        return {'graphs': service.graphs()}

    @api.get('/api/executions')
    def executions(limit: int = 30):
        return {'runs': service.execution_runs(limit)}

    @api.get('/api/executions/{run_id}')
    def execution(run_id: str):
        try:
            return service.execution(run_id)
        except UnknownSessionError as error:
            raise HTTPException(404, detail='Unknown execution') from error

    @api.get('/api/operator-chat')
    def operator_history(request: Request):
        user_id, _, location_id = operator_identity(request)
        return {**service.operator_history(user_id, location_id),
                'access': 'write' if operator_can_write(request) else 'read'}

    @api.get('/api/console/chat-tools')
    def console_chat_tools():
        return service.chat_tool_catalog()

    @api.get('/api/console/operator-history/{console_id}')
    def console_operator_history(request: Request, console_id: str):
        user_id, location_id = console_identity(request, console_id)
        return {**service.operator_history(user_id, location_id),
                'traced_message_ids': OperatorChatStore(
                    service.config.state_path).traced_sequences(user_id, location_id)}

    @api.get('/api/console/run-trace/{console_id}/{message_id}')
    def console_run_trace(request: Request, console_id: str, message_id: int):
        user_id, location_id = console_identity(request, console_id)
        trace = OperatorChatStore(service.config.state_path).trace(
            user_id, location_id, message_id)
        if trace is None:
            raise HTTPException(404, detail='No run trace was saved for this message')
        return trace

    @api.delete('/api/console/operator-history/{console_id}')
    def console_operator_reset(request: Request, console_id: str):
        user_id, location_id = console_identity(request, console_id)
        OperatorChatStore(service.config.state_path).reset(user_id, location_id)
        return {'messages': []}

    @api.post('/api/console/operator-focus')
    def console_operator_focus(request: Request, body: ConsoleOperatorFocus):
        console_identity(request, body.console_id)
        try:
            return service.console_focus_operator_shipment(body.console_id,
                                                           body.shipment_id,
                                                           body.shipment_number)
        except (KeyError, ValueError, LookupError) as error:
            raise HTTPException(404, detail='Shipment is unavailable in this console') from error

    @api.get('/api/console/operator-progress/{console_id}/{request_id}')
    def console_operator_progress(request: Request, console_id: str, request_id: str):
        user_id, location_id = console_identity(request, console_id)
        try:
            uuid.UUID(request_id)
        except ValueError as error:
            raise HTTPException(400, detail='Invalid request ID') from error
        return OperatorChatStore(service.config.state_path).progress(
            request_id, user_id, location_id) or {}

    @api.websocket('/api/console/operator-progress/ws/{console_id}/{request_id}')
    async def console_operator_progress_socket(websocket: WebSocket, console_id: str,
                                               request_id: str):
        try:
            user_id, location_id = console_identity(websocket, console_id)
            if str(uuid.UUID(request_id)) != request_id:
                raise ValueError('Invalid request ID')
        except (ValueError, HTTPException):
            await websocket.close(code=1008)
            return
        await stream_operator_progress(websocket, request_id, user_id, location_id)

    @api.post('/api/console/operator-message')
    def console_operator_message(request: Request, body: ConsoleOperatorMessage):
        user_id, location_id = console_identity(request, body.console_id)
        if not service.config.openrouter_key:
            raise HTTPException(503, detail='OpenRouter is not configured')
        if body.request_id:
            try:
                uuid.UUID(body.request_id)
                OperatorChatStore(service.config.state_path).begin_progress(
                    body.request_id, user_id, location_id)
            except (ValueError, sqlite3.IntegrityError) as error:
                raise HTTPException(400, detail='Invalid request ID') from error
        store = OperatorChatStore(service.config.state_path)
        try:
            with observe_progress(lambda graph_id, node, message: store.update_progress(
                    body.request_id, user_id, location_id, node,
                    graph_id=graph_id, message=message)):
                return service.console_operator_message(body.console_id, body.text,
                                                        body.request_id, body.access)
        except PermissionError as error:
            store.update_progress(body.request_id, user_id, location_id, 'failed')
            raise HTTPException(403, detail=str(error)) from error
        except ValueError as error:
            store.update_progress(body.request_id, user_id, location_id, 'failed')
            raise HTTPException(422, detail=str(error)) from error
        except Exception as error:
            store.update_progress(body.request_id, user_id, location_id, 'failed')
            raise HTTPException(502, detail='Local console chat failed: ' +
                                type(error).__name__) from error

    @api.delete('/api/operator-chat')
    def reset_operator_history(request: Request):
        user_id, _, location_id = operator_identity(request)
        OperatorChatStore(service.config.state_path).reset(user_id, location_id)
        return {'messages': []}

    @api.post('/api/operator-chat/focus')
    def focus_operator_chat(request: Request, body: OperatorFocus):
        user_id, _, location_id = operator_identity(request)
        try:
            return service.focus_operator_shipment(user_id, location_id,
                                                   body.shipment_number,
                                                   body.shipment_id)
        except ValueError as error:
            raise HTTPException(422, detail=str(error)) from error

    @api.post('/api/operator-chat/progress/start')
    def start_operator_progress(request: Request, body: ChatProgressStart):
        user_id, _, location_id = operator_identity(request)
        try:
            uuid.UUID(body.request_id)
            OperatorChatStore(service.config.state_path).begin_progress(
                body.request_id, user_id, location_id)
        except (ValueError, sqlite3.IntegrityError) as error:
            raise HTTPException(400, detail='Invalid chat request ID') from error
        return OperatorChatStore(service.config.state_path).progress(
            body.request_id, user_id, location_id)

    @api.post('/api/operator-chat/progress/ticket')
    def operator_progress_ticket(request: Request, body: ChatProgressStart):
        user_id, _, location_id = operator_identity(request)
        try:
            if str(uuid.UUID(body.request_id)) != body.request_id:
                raise ValueError('Invalid request ID')
        except ValueError as error:
            raise HTTPException(400, detail='Invalid chat request ID') from error
        return {'ticket': progress_ticket(service.config.openboxes_sidecar_token,
                                          body.request_id, user_id, location_id)}

    @api.websocket('/api/operator-chat/progress/ws/{request_id}')
    async def operator_progress_socket(websocket: WebSocket, request_id: str):
        protocols = [value.strip() for value in
                     websocket.headers.get('sec-websocket-protocol', '').split(',')]
        ticket = next((value[len('ticket.'):] for value in protocols
                       if value.startswith('ticket.')), '')
        claims = verify_progress_ticket(
            service.config.openboxes_sidecar_token,
            ticket, request_id)
        if 'operator-progress' not in protocols or not claims:
            await websocket.close(code=1008)
            return
        await stream_operator_progress(websocket, request_id,
                                       claims['user_id'], claims['location_id'],
                                       subprotocol='operator-progress')

    @api.post('/api/operator-chat/progress/stage')
    def stage_operator_progress(request: Request, body: ChatProgressStage):
        user_id, _, location_id = operator_identity(request)
        allowed_sources = {
            'openboxes_shipment_lookup': ([], ['OpenBoxes shipment']),
            'fetch_needs_attention': ([],),
            'fetch_delayed_shipments': ([], ['OpenBoxes delayed incoming shipments']),
        }
        if body.stage not in allowed_sources or \
                body.sources not in allowed_sources[body.stage]:
            raise HTTPException(400, detail='Invalid chat progress stage')
        store = OperatorChatStore(service.config.state_path)
        reference = body.shipment_reference
        message = ('Reading Needs Attention items from OpenBoxes' if
                   body.stage == 'fetch_needs_attention' else
                   'Reading delayed incoming shipments from OpenBoxes' if
                   body.stage == 'fetch_delayed_shipments' else
                   f'Looking up {reference} in OpenBoxes' if reference else
                   'Checking shipment access in OpenBoxes')
        store.update_progress(body.request_id, user_id, location_id,
                              body.stage, body.sources, graph_id='openboxes',
                              message=message)
        return store.progress(body.request_id, user_id, location_id) or {}

    @api.get('/api/operator-chat/progress/{request_id}')
    def operator_progress(request: Request, request_id: str):
        user_id, _, location_id = operator_identity(request)
        return OperatorChatStore(service.config.state_path).progress(
            request_id, user_id, location_id) or {}

    @api.post('/api/operator-chat/resolve')
    def resolve_operator_reference(request: Request, body: OperatorMessage):
        user_id, _, location_id = operator_identity(request)
        if not service.config.openrouter_key:
            raise HTTPException(503, detail='OpenRouter is not configured')
        try:
            store = OperatorChatStore(service.config.state_path)
            with observe_progress(lambda graph_id, node, message: store.update_progress(
                    body.request_id, user_id, location_id, node,
                    graph_id=graph_id, message=message)):
                return service.resolve_operator_reference(user_id, location_id, body.text)
        except ValueError as error:
            OperatorChatStore(service.config.state_path).update_progress(
                body.request_id, user_id, location_id, 'failed',
                message='Could not verify the request details')
            raise HTTPException(422, detail=str(error)) from error
        except Exception as error:
            OperatorChatStore(service.config.state_path).update_progress(
                body.request_id, user_id, location_id, 'failed',
                message='Shipment extraction failed')
            raise HTTPException(502, detail='Shipment extraction failed: ' +
                                type(error).__name__) from error

    @api.post('/api/operator-chat')
    def operator_message(request: Request, body: OperatorMessage):
        user_id, username, location_id = operator_identity(request)
        if not service.config.openrouter_key:
            raise HTTPException(503, detail='OpenRouter is not configured')
        try:
            store = OperatorChatStore(service.config.state_path)
            with observe_progress(lambda graph_id, node, message: store.update_progress(
                    body.request_id, user_id, location_id, node,
                    graph_id=graph_id, message=message)):
                return service.operator_message(user_id, username, location_id,
                                                body.text, body.shipment_context,
                                                body.resolution_note, body.request_id,
                                                intent_type=body.intent_type,
                                                entity_source_message_id=body.entity_source_message_id,
                                                contact_role=body.contact_role,
                                                attention_context=body.attention_context,
                                                shipment_search_context=body.shipment_search_context,
                                                excluded_shipment_reference=body.excluded_shipment_reference,
                                                can_write=operator_can_write(request),
                                                recall_context=body.recall_context,
                                                tow_service_reference=body.tow_service_reference,
                                                data_ticket=body.data_ticket)
        except PermissionError as error:
            OperatorChatStore(service.config.state_path).update_progress(
                body.request_id, user_id, location_id, 'failed')
            raise HTTPException(403, detail=str(error)) from error
        except ValueError as error:
            OperatorChatStore(service.config.state_path).update_progress(
                body.request_id, user_id, location_id, 'failed')
            raise HTTPException(400, detail=str(error)) from error
        except Exception as error:
            OperatorChatStore(service.config.state_path).update_progress(
                body.request_id, user_id, location_id, 'failed')
            raise HTTPException(502, detail='Operator chat failed: ' +
                                type(error).__name__) from error

    @api.post('/api/sessions/from-message')
    def create_session_from_message(body: ChatMessage):
        try:
            return service.create_session_from_message(body.text)
        except ValueError as error:
            raise HTTPException(422, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(503, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(502, detail='Shipment resolution failed: ' +
                                type(error).__name__) from error

    @api.post('/api/sessions')
    def create_session(body: NewSession):
        try:
            return service.create_session(body.shipment_id)
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(502, detail='OpenBoxes read failed: ' + type(error).__name__) from error

    @api.post('/api/sessions/sample')
    def create_sample_session():
        return service.create_sample_session()

    @api.get('/api/sessions/{session_id}')
    def get_session(session_id: str):
        try:
            return service.snapshot(session_id)
        except UnknownSessionError as error:
            raise HTTPException(404, detail='Unknown session') from error

    @api.post('/api/sessions/{session_id}/recovery-replies')
    def check_recovery_replies(session_id: str):
        try:
            return service.check_recovery_replies(session_id, apply=True)
        except UnknownSessionError as error:
            raise HTTPException(404, detail='Unknown session') from error
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(503, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(502, detail='Recovery reply check failed: ' +
                                type(error).__name__) from error

    @api.post('/api/sessions/{session_id}/dispatch-request')
    def request_recovery_dispatch(session_id: str, body: DispatchApproval):
        try:
            return service.request_recovery_dispatch(session_id, body.model_dump())
        except UnknownSessionError as error:
            raise HTTPException(404, detail='Unknown session') from error
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(503, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(502, detail='Dispatch request failed: ' +
                                type(error).__name__) from error

    @api.post('/api/sessions/{session_id}/messages')
    def send_message(session_id: str, body: ChatMessage):
        try:
            return service.send_message(session_id, body.text, body.image_data_url)
        except UnknownSessionError as error:
            raise HTTPException(404, detail='Unknown session') from error
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(503, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(502, detail='Workflow failed: ' + type(error).__name__) from error

    @api.post('/api/sessions/{session_id}/messages/stream')
    def stream_message(session_id: str, body: ChatMessage):
        try:
            service.validate_message(session_id, body.text, body.image_data_url)
        except UnknownSessionError as error:
            raise HTTPException(404, detail='Unknown session') from error
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(503, detail=str(error)) from error

        def events():
            try:
                for kind, payload in service.stream_message(
                        session_id, body.text, body.image_data_url):
                    yield 'event: ' + kind + '\ndata: ' + json.dumps(payload, default=str) + '\n\n'
            except Exception as error:
                yield ('event: error\ndata: ' + json.dumps({
                    'detail': 'Workflow failed: ' + type(error).__name__,
                    'error_type': type(error).__name__}) + '\n\n')

        return StreamingResponse(events(), media_type='text/event-stream', headers={
            'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'})

    return api


app = create_app()
