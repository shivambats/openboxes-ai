import React, { Component } from 'react';

import PropTypes from 'prop-types';

import ChatComponent, { Insights, NextSteps } from 'components/dashboard/ChatComponents';
import apiClient from 'utils/apiClient';
import { renderChatMarkdown } from 'utils/chatMarkdown';

import 'components/dashboard/OperatorChatPanel.scss';

function SourceTag({ source }) {
  const date = source.received_at && new Date(source.received_at);
  const time = date && !Number.isNaN(date.getTime())
    ? `${date.toLocaleString('en-GB', {
      day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC',
    })} UTC`
    : '';
  return (
    <span className="operator-chat-source">
      {`Source: ${source.kind}${time ? ` · received ${time}` : ''}`}
    </span>
  );
}

SourceTag.propTypes = {
  source: PropTypes.shape({
    kind: PropTypes.string,
    received_at: PropTypes.string,
    message_id: PropTypes.string,
  }).isRequired,
};

// Cards with their own row in a message; the rest share the carrier and shipment row.
const OWN_ROW_CARDS = new Set(['component', 'next_steps', 'insights']);

// Record links point at OpenBoxes pages. A plain click opens the page beside the chat
// (onOpen); a modified click (new tab, new window) is left to the browser.
function recordLinkHandler(onOpen) {
  return (event) => {
    const link = event.target.closest?.('a');
    const href = link?.getAttribute('href') || '';
    if (!href.startsWith('/openboxes/') || event.defaultPrevented) return;
    const modified = event.metaKey || event.ctrlKey || event.shiftKey || event.altKey;
    if (event.button !== 0 || modified) return;
    event.preventDefault();
    if (onOpen) onOpen(href);
    else window.open(href, '_blank', 'noopener,noreferrer');
  };
}

function MessageSources({ annotations }) {
  const sources = new Map();
  annotations.forEach((item) => (item.sources || []).forEach((source) => {
    if (source.kind) sources.set(`${source.kind}:${source.message_id || source.received_at || ''}`, source);
  }));
  if (!sources.size) return null;
  return (
    <div className="operator-chat-sources">
      {[...sources].map(([key, source]) => <SourceTag source={source} key={key} />)}
    </div>
  );
}

MessageSources.propTypes = { annotations: PropTypes.arrayOf(PropTypes.object) };
MessageSources.defaultProps = { annotations: [] };

function shipmentActionHeight(cards) {
  const rows = Math.max(1, ...cards.filter((card) => card.type === 'delayed_shipment')
    .map((card) => Math.ceil(Math.max(1, card.actions?.length || 1) / 2)));
  return `${rows * 42 + (rows - 1) * 7}px`;
}

function CarrierCard({ card, disabled, onAction }) {
  const complete = card.quote_status === 'complete';
  const action = complete
    ? `Select ${card.name} for fleet review on ${card.shipment_number}`
    : `Follow up with ${card.name} on quote status for ${card.shipment_number}`;
  let actionLabel = 'Request quote details';
  if (card.selected_by_operator) actionLabel = 'Continue carrier and driver emails';
  else if (card.can_select) actionLabel = 'Choose this carrier';
  return (
    <article className="operator-chat-carrier">
      <div className="operator-chat-carrier-heading">
        <strong>{card.name}</strong>
        <span className={`operator-chat-card-status ${complete ? 'is-complete' : ''}`}>
          {complete ? 'Complete quote' : card.quote_status.split('_').join(' ')}
        </span>
      </div>
      {card.price_usd && <p>{`${card.mode || 'Mode unconfirmed'} · USD ${card.price_usd}`}</p>}
      {complete && <p>{`Pickup ${card.pickup_eta_hours} h · Delivery ${card.delivery_eta_hours} h after approval`}</p>}
      <p className="operator-chat-carrier-prior">
        {typeof card.reliability_prior === 'number'
          ? `${Math.round(card.reliability_prior * 100)}% synthetic reliability prior`
          : 'Reliability estimate unavailable'}
      </p>
      <p className="operator-chat-carrier-basis">No verified past delivery history is on file.</p>
      {card.source_kind && (
      <SourceTag source={{
        kind: card.source_kind,
        received_at: card.source_received_at,
        message_id: card.source_message_id,
      }}
      />
      )}
      {card.unreviewed_reply && <p>Newer reply awaiting review.</p>}
      {card.selected_by_operator && (
        <span className="operator-chat-carrier-selected">Chosen carrier</span>
      )}
      {(!card.selected_by_operator || !card.dispatch_steps_complete) && (
        <button
          type="button"
          className={`operator-chat-card-button ${card.can_select || card.selected_by_operator ? 'is-primary' : 'is-secondary'}`}
          onClick={() => onAction(action)}
          disabled={disabled}
        >
          {actionLabel}
        </button>
      )}
    </article>
  );
}

CarrierCard.propTypes = {
  card: PropTypes.shape({
    name: PropTypes.string,
    shipment_number: PropTypes.string,
    quote_status: PropTypes.string,
    price_usd: PropTypes.string,
    mode: PropTypes.string,
    pickup_eta_hours: PropTypes.string,
    delivery_eta_hours: PropTypes.string,
    reliability_prior: PropTypes.number,
    unreviewed_reply: PropTypes.bool,
    selected_by_operator: PropTypes.bool,
    dispatch_steps_complete: PropTypes.bool,
    can_select: PropTypes.bool,
    source_kind: PropTypes.string,
    source_received_at: PropTypes.string,
    source_message_id: PropTypes.string,
  }).isRequired,
  disabled: PropTypes.bool,
  onAction: PropTypes.func.isRequired,
};

CarrierCard.defaultProps = { disabled: false };

function TowServiceCard({ card, disabled, onSave }) {
  return (
    <article className="operator-chat-shipment-card operator-chat-tow-card">
      <div className="operator-chat-shipment-topline">
        <span className="operator-chat-shipment-status">Tow service lead</span>
        <span className="operator-chat-shipment-source">Google Maps</span>
      </div>
      <strong className="operator-chat-shipment-number">{card.name}</strong>
      <p className="operator-chat-tow-address">{card.address}</p>
      <div className="operator-chat-tow-facts">
        {card.phone && (
          <p>
            <span>Phone</span>
            <strong>{card.phone}</strong>
          </p>
        )}
        {card.miles != null && (
          <p>
            <span>From scene</span>
            <strong>{`${card.miles} mi to listed address`}</strong>
          </p>
        )}
        {card.published_rate && (
          <p>
            <span>Published rate</span>
            <strong>{card.published_rate}</strong>
          </p>
        )}
        <p>
          <span>Quote</span>
          <strong>{card.quote_status.split('_').join(' ')}</strong>
        </p>
      </div>
      {!card.heavy_duty_verified && <p className="operator-chat-tow-note">Heavy duty capability unconfirmed</p>}
      {card.reply_excerpt && <p className="operator-chat-tow-reply">{card.reply_excerpt}</p>}
      <div className="operator-chat-card-footer">
        <div className="operator-chat-tow-links">
          {card.google_maps_url && <a href={card.google_maps_url} target="_blank" rel="noopener noreferrer">Google Maps</a>}
          {card.website && <a href={card.website} target="_blank" rel="noopener noreferrer">Website</a>}
          {card.miles != null && card.distance_source && (
            <a href={card.distance_source} target="_blank" rel="noopener noreferrer">Route estimate</a>
          )}
        </div>
        {card.quote_status === 'replied' && !card.saved_vendor && (
          <button type="button" className="operator-chat-card-button is-primary" onClick={() => onSave(card)} disabled={disabled}>
            Save as towing vendor
          </button>
        )}
        {card.saved_vendor && <span className="operator-chat-carrier-selected">Saved · pending fleet vetting</span>}
      </div>
    </article>
  );
}

TowServiceCard.propTypes = {
  card: PropTypes.shape({
    name: PropTypes.string,
    address: PropTypes.string,
    phone: PropTypes.string,
    heavy_duty_verified: PropTypes.bool,
    miles: PropTypes.number,
    distance_source: PropTypes.string,
    website: PropTypes.string,
    published_rate: PropTypes.string,
    quote_status: PropTypes.string,
    reply_excerpt: PropTypes.string,
    google_maps_url: PropTypes.string,
    saved_vendor: PropTypes.bool,
  }).isRequired,
  disabled: PropTypes.bool,
  onSave: PropTypes.func.isRequired,
};

TowServiceCard.defaultProps = { disabled: false };

function DelayedShipmentCard({
  card, disabled, readOnly, onFocus,
}) {
  const actions = card.actions?.length ? card.actions
    : [{ id: 'focus', label: 'Focus on shipment' }];
  return (
    <article className="operator-chat-shipment-card" role="listitem">
      <div className="operator-chat-shipment-topline">
        <span className="operator-chat-shipment-status">Delayed incoming</span>
        <span className="operator-chat-shipment-source">OpenBoxes</span>
      </div>
      <strong className="operator-chat-shipment-number">{card.number}</strong>
      <div className="operator-chat-shipment-route" aria-label={`Route: ${card.origin} to ${card.destination}`}>
        <span>{card.origin}</span>
        <span className="operator-chat-shipment-arrow" aria-hidden="true">→</span>
        <span>{card.destination}</span>
      </div>
      <div className="operator-chat-shipment-arrival">
        <span>Expected arrival</span>
        <strong>{card.arrival_label}</strong>
      </div>
      <div className="operator-chat-shipment-actions">
        {actions.map((action) => (
          <button
            type="button"
            key={action.id}
            onClick={() => onFocus(card, action)}
            disabled={disabled || (readOnly && action.id !== 'focus'
              && !['inspection_status', 'load_transfer_status', 'check_quotes'].includes(action.id))}
            className="operator-chat-card-button is-secondary"
          >
            {action.label}
          </button>
        ))}
      </div>
    </article>
  );
}

DelayedShipmentCard.propTypes = {
  card: PropTypes.shape({
    number: PropTypes.string,
    shipment_id: PropTypes.string,
    origin: PropTypes.string,
    destination: PropTypes.string,
    arrival_label: PropTypes.string,
    actions: PropTypes.arrayOf(PropTypes.shape({
      id: PropTypes.string.isRequired,
      label: PropTypes.string.isRequired,
      prompt: PropTypes.string,
    })),
  }).isRequired,
  disabled: PropTypes.bool,
  readOnly: PropTypes.bool,
  onFocus: PropTypes.func.isRequired,
};

DelayedShipmentCard.defaultProps = { disabled: false, readOnly: false };

class OperatorChatPanel extends Component {
  constructor(props) {
    super(props);
    this.messagesRef = React.createRef();
    this.progressSocket = null;
    this.activeRequestId = null;
    this.state = {
      messages: [],
      draft: '',
      loading: true,
      sending: false,
      focusing: false,
      resetting: false,
      error: '',
      progress: null,
      access: 'read',
      recallNotices: [],
    };
  }

  componentDidMount() { this.loadHistory(); }

  componentWillUnmount() { this.stopProgress(); }

  componentDidUpdate(prevProps, prevState) {
    if (prevProps.userId !== this.props.userId
        || prevProps.locationId !== this.props.locationId) {
      this.loadHistory();
      return;
    }
    const log = this.messagesRef.current;
    if (!log) return;
    const previousDivider = [...prevState.messages].reverse()
      .find((item) => item.role === 'divider');
    const currentDivider = [...this.state.messages].reverse()
      .find((item) => item.role === 'divider');
    if (!prevState.loading && currentDivider && currentDivider.id !== previousDivider?.id
      && this.latestDividerRef) {
      log.scrollTop += this.latestDividerRef.getBoundingClientRect().top
        - log.getBoundingClientRect().top - 8;
      return;
    }
    const previousAssistant = [...prevState.messages].reverse()
      .find((item) => item.role === 'assistant');
    const currentAssistant = [...this.state.messages].reverse()
      .find((item) => item.role === 'assistant');
    if (currentAssistant && currentAssistant.id !== previousAssistant?.id
      && this.latestAssistantRef) {
      log.scrollTop += this.latestAssistantRef.getBoundingClientRect().top
        - log.getBoundingClientRect().top - 8;
    } else if (this.state.messages.length > prevState.messages.length
      && this.state.messages[this.state.messages.length - 1]?.pending) {
      log.scrollTop = log.scrollHeight;
    }
  }

  stopProgress = () => {
    if (this.progressSocket) {
      this.progressSocket.onmessage = null;
      this.progressSocket.onclose = null;
      this.progressSocket.close();
    }
    this.progressSocket = null;
    this.activeRequestId = null;
  };

  watchProgress = (requestId) => {
    if (!requestId) return;
    this.stopProgress();
    this.activeRequestId = requestId;
    apiClient.get(`/api/operator-chat/progress-ticket/${requestId}`)
      .then(({ data }) => {
        if (this.activeRequestId !== requestId || !data?.ticket) return;
        const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const contextPath = (window.CONTEXT_PATH || '/openboxes').replace(/\/$/, '');
        const url = `${scheme}//${window.location.host}${contextPath}/api/operator-chat/progress/ws/${requestId}`;
        const socket = new WebSocket(url, ['operator-progress', `ticket.${data.ticket}`]);
        this.progressSocket = socket;
        socket.onmessage = ({ data: payload }) => {
          if (this.activeRequestId !== requestId) return;
          try {
            const progress = JSON.parse(payload);
            if (progress?.message) this.setState({ progress });
          } catch (ignored) { /* The final chat response still arrives by HTTP. */ }
        };
        socket.onclose = () => {
          if (this.progressSocket === socket) this.progressSocket = null;
        };
      })
      .catch(() => {});
  };

  loadHistory = () => {
    this.stopProgress();
    if (!this.props.userId) {
      this.setState({
        messages: [],
        loading: false,
        sending: false,
        focusing: false,
        error: '',
        progress: null,
      });
      return;
    }
    const { userId, locationId } = this.props;
    this.setState({
      loading: true,
      sending: false,
      focusing: false,
      messages: [],
      error: '',
      progress: null,
    });
    apiClient.get('/api/operator-chat')
      .then(({ data }) => {
        if (userId === this.props.userId && locationId === this.props.locationId) {
          this.setState({
            messages: data.messages || [],
            recallNotices: data.recall_notices || [],
            access: data.access || 'read',
            loading: false,
          });
        }
      })
      .catch(({ response }) => {
        if (userId === this.props.userId && locationId === this.props.locationId) {
          this.setState({
            loading: false,
            access: response?.status === 403 ? 'none' : 'read',
            error: response?.status === 403 ? 'You do not have access to operations chat.' : 'Chat is unavailable.',
          });
        }
      });
  };

  sendText = (message, fromDraft = false) => {
    const { draft, sending, focusing } = this.state;
    const { userId, locationId } = this.props;
    const text = message.trim();
    if (!text || sending || focusing) return;
    const requestId = window.crypto?.randomUUID?.() || null;
    const pending = {
      id: `pending-${Date.now()}`, role: 'user', text, pending: true,
    };
    this.setState((state) => ({
      messages: [...state.messages, pending],
      draft: fromDraft ? '' : state.draft,
      sending: true,
      error: '',
      progress: null,
    }));
    this.watchProgress(requestId);
    apiClient.post('/api/operator-chat', { text, request_id: requestId })
      .then(({ data }) => {
        this.stopProgress();
        if (userId === this.props.userId && locationId === this.props.locationId) {
          this.setState({
            messages: data.messages || [],
            recallNotices: data.recall_notices || [],
            sending: false,
            progress: null,
          });
        }
      })
      .catch(({ response }) => {
        this.stopProgress();
        if (userId === this.props.userId && locationId === this.props.locationId) {
          this.setState((state) => ({
            messages: state.messages.filter((item) => item.id !== pending.id),
            draft: fromDraft ? draft : state.draft,
            sending: false,
            progress: null,
            error: response?.data?.error || response?.data?.detail || 'Could not send the message.',
          }));
        }
      });
  };

  focusShipment = (card, action = null) => {
    const { sending, focusing, loading } = this.state;
    const { userId, locationId } = this.props;
    if (sending || focusing || loading || (!card.shipment_id && !card.number)) return;
    this.setState({ focusing: true, error: '' });
    apiClient.post('/api/operator-chat/focus', {
      shipment_id: card.shipment_id,
      shipment_number: card.number,
    })
      .then(({ data }) => {
        if (userId === this.props.userId && locationId === this.props.locationId) {
          this.setState({ messages: data.messages || [], focusing: false }, () => {
            if (action?.prompt) this.sendText(action.prompt);
          });
        }
      })
      .catch(({ response }) => {
        if (userId === this.props.userId && locationId === this.props.locationId) {
          this.setState({
            focusing: false,
            error: response?.data?.error || 'Could not start a new shipment context.',
          });
        }
      });
  };

  send = (event) => {
    event.preventDefault();
    this.sendText(this.state.draft, true);
  };

  resetChat = () => {
    if (this.state.sending || this.state.focusing
      || this.state.resetting || this.state.loading) return;
    if (!window.confirm('Clear this chat history? Saved shipment cases and sent emails will remain.')) return;
    this.setState({ resetting: true, error: '' });
    apiClient.delete('/api/operator-chat')
      .then(() => {
        this.stopProgress();
        this.setState({
          messages: [], draft: '', progress: null, resetting: false, focusing: false,
        });
      })
      .catch(() => this.setState({
        resetting: false, error: 'Could not reset the chat. Please try again.',
      }));
  };

  renderProgress() {
    const { progress } = this.state;
    return (
      <div className="operator-chat-progress" role="status" aria-live="polite">
        <span className="operator-chat-progress-spinner" aria-hidden="true" />
        <span>{progress?.message || 'Waiting for agent activity…'}</span>
      </div>
    );
  }

  render() {
    const { username } = this.props;
    const {
      messages, draft, loading, sending, focusing, error, access, recallNotices,
    } = this.state;
    // Only the newest answer's suggestions can be sent; older ones may be out of date.
    const latestAssistantId = [...messages].reverse().find((item) => item.role === 'assistant')?.id;
    return (
      // Record links anywhere in the chat open beside it; see recordLinkHandler.
      // eslint-disable-next-line max-len
      // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions
      <aside className="operator-chat-panel" aria-labelledby="operator-chat-title" onClick={recordLinkHandler(this.props.onOpenRecord)}>
        <div className="operator-chat-heading">
          <span className="operator-chat-kicker">OPENBOXES ASSISTANT</span>
          <h2 id="operator-chat-title">Your operations chat</h2>
          <button
            type="button"
            className="operator-chat-reset"
            onClick={this.resetChat}
            disabled={sending || focusing || loading
              || this.state.resetting || messages.length === 0}
          >
            {this.state.resetting ? 'Clearing…' : 'Reset chat'}
          </button>
          <p>
            Signed in as&nbsp;
            <strong>{username || 'your account'}</strong>
          </p>
        </div>
        <div className="operator-chat-messages" role="log" aria-live="polite" ref={this.messagesRef}>
          {!!recallNotices.length && (
            <div className="operator-chat-recall-notices" aria-label="Warehouse recall notices">
              <strong>Warehouse recall notices</strong>
              {recallNotices.map((notice) => (
                <article key={`${notice.source_url}:${notice.lot_number}`}>
                  <strong>{notice.product_name}</strong>
                  <span>{`Lot ${notice.lot_number} · bins ${notice.bins}`}</span>
                  <a href={notice.source_url} target="_blank" rel="noopener noreferrer">FDA source</a>
                </article>
              ))}
            </div>
          )}
          {loading && <p className="operator-chat-empty">Loading your conversation…</p>}
          {!loading && messages.length === 0 && (
            <p className="operator-chat-empty">Ask about a shipment or the next step in a workflow.</p>
          )}
          {messages.map((message) => (message.role === 'divider' ? (
            <div
              className="operator-chat-context-divider"
              key={message.id}
              ref={(node) => { this.latestDividerRef = node; }}
              role="separator"
              aria-label={message.text}
            >
              <span>{message.text}</span>
            </div>
          ) : (
            <div
              className={`operator-chat-message ${message.role}`}
              key={message.id}
              ref={(node) => { if (message.role === 'assistant') this.latestAssistantRef = node; }}
            >
              <span>{message.role === 'user' ? 'You' : 'Assistant'}</span>
              <div className="operator-chat-bubble">
                {/* Findings replace the reply text when the answer has them. */}
                {(message.cards || []).filter((card) => card.type === 'insights').map((card) => (
                  <Insights
                    card={card}
                    key="insights"
                    onAsk={this.sendText}
                    disabled={sending || focusing || message.id !== latestAssistantId}
                  />
                ))}
                {!message.cards?.some((card) => card.type === 'insights') && (
                <div
                  className="operator-chat-richtext"
                  dangerouslySetInnerHTML={{ __html: renderChatMarkdown(message.text) }}
                />
                )}
              </div>
              <MessageSources annotations={message.annotations} />
              {(message.cards || []).filter((card) => card.type === 'component').map((card, index) => (
                // eslint-disable-next-line react/no-array-index-key
                <ChatComponent card={card} key={`${card.component}-${index}`} />
              ))}
              {(message.cards || []).filter((card) => card.type === 'next_steps').map((card) => (
                <NextSteps
                  card={card}
                  key="next-steps"
                  onAsk={this.sendText}
                  disabled={sending || focusing || message.id !== latestAssistantId}
                />
              ))}
              {!!message.cards?.some((card) => !OWN_ROW_CARDS.has(card.type)) && (
                <div
                  className="operator-chat-carriers"
                  style={{ '--shipment-action-height': shipmentActionHeight(message.cards) }}
                  role={message.cards.every((card) => card.type === 'delayed_shipment') ? 'list' : undefined}
                  aria-label={message.cards.every((card) => card.type === 'delayed_shipment')
                    ? 'Delayed incoming shipments' : undefined}
                >
                  {message.cards.filter((card) => !OWN_ROW_CARDS.has(card.type)).map((card) => {
                    if (card.type === 'delayed_shipment') {
                      return (
                        <DelayedShipmentCard
                          card={card}
                          key={card.shipment_id || card.number}
                          onFocus={this.focusShipment}
                          disabled={sending || focusing}
                          readOnly={access !== 'write'}
                        />
                      );
                    }
                    if (card.type === 'tow_service') {
                      return (
                        <TowServiceCard
                          card={card}
                          key={card.place_id}
                          onSave={(lead) => this.focusShipment(lead, {
                            prompt: `Save ${lead.name} (${lead.address}) as a towing vendor.`,
                          })}
                          disabled={sending || focusing || access !== 'write'}
                        />
                      );
                    }
                    return (
                      <CarrierCard
                        card={card}
                        key={card.vendor_id}
                        onAction={this.sendText}
                        disabled={sending || access !== 'write'}
                      />
                    );
                  })}
                </div>
              )}
            </div>
          )))}
          {sending && this.renderProgress()}
        </div>
        <form className="operator-chat-form" onSubmit={this.send}>
          <label htmlFor="operator-message">Message</label>
          <textarea
            id="operator-message"
            value={draft}
            disabled={access === 'none'}
            onChange={(event) => this.setState({ draft: event.target.value })}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                this.send(event);
              }
            }}
            maxLength={4000}
            rows={3}
            placeholder="What should I check for SHIP-2026-API1?"
          />
          {error && <p className="operator-chat-error" role="alert">{error}</p>}
          <button
            type="submit"
            disabled={sending || focusing || loading || access === 'none'
            || !this.props.userId || !draft.trim()}
          >
            {sending ? 'Working…' : 'Send message'}
          </button>
        </form>
      </aside>
    );
  }
}

OperatorChatPanel.propTypes = {
  userId: PropTypes.string,
  username: PropTypes.string,
  locationId: PropTypes.string,
  onOpenRecord: PropTypes.func,
};

OperatorChatPanel.defaultProps = {
  userId: '', username: '', locationId: '', onOpenRecord: null,
};

export default OperatorChatPanel;
