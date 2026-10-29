import React, { useState } from 'react';

import PropTypes from 'prop-types';

import { renderChatMarkdown } from 'utils/chatMarkdown';

import 'components/dashboard/ChatComponents.scss';

// Interactive chat components. The sidecar builds each card from tool data and Jev
// chooses which component fits; these views only display what the card holds.
const ICONS = {
  timeline: '◷', email_thread: '✉', table: '▦', bar_chart: '▮', stat_cards: '#', detail: '≡',
};

export function formatCell(type, value) {
  if (value === null || value === undefined || value === '') return '—';
  if (type === 'date') {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return `${date.toLocaleString('en-GB', {
      day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC',
    })} UTC`;
  }
  if (type === 'number' && !Number.isNaN(Number(value))) return Number(value).toLocaleString();
  return String(value).replace(/_/g, ' ');
}

// Links come from the sidecar, built from OpenBoxes ids and fixed page templates; only
// OpenBoxes pages are ever linked.
export function recordHref(card, entity, value) {
  if (!entity || typeof value !== 'string') return null;
  const href = card.links?.[value];
  return typeof href === 'string' && href.startsWith('/openboxes/') ? href : null;
}

function RecordValue({
  card, entity, value, type,
}) {
  const href = recordHref(card, entity, value);
  const text = formatCell(type, value);
  return href ? (
    <a className="chat-record-link" href={href} target="_blank" rel="noopener noreferrer" title={`Open ${text} in OpenBoxes`}>
      {text}
    </a>
  ) : text;
}

function Timeline({ card }) {
  const tags = [...new Set(card.items.map((item) => item.tag).filter(Boolean))];
  const [active, setActive] = useState(null);
  const [open, setOpen] = useState({});
  return (
    <>
      {tags.length > 1 && (
        <div className="chat-filters">
          {[null, ...tags].map((tag) => (
            <button
              type="button"
              key={tag || 'all'}
              className={active === tag ? 'active' : ''}
              onClick={() => setActive(tag)}
            >
              {tag || 'All'}
            </button>
          ))}
        </div>
      )}
      <ol className="chat-timeline">
        {card.items.filter((item) => !active || item.tag === active).map((item, index) => (
          // eslint-disable-next-line react/no-array-index-key
          <li key={`${item.at}-${index}`} className={`tag-${(item.tag || 'other').replace(/ /g, '-')}`}>
            <time>{formatCell('date', item.at)}</time>
            <div className="chat-timeline-title">{item.title}</div>
            {item.tag && <span className="chat-timeline-tag">{item.tag}</span>}
            {item.detail && (
              <>
                <button
                  type="button"
                  className="chat-link"
                  onClick={() => setOpen({ ...open, [index]: !open[index] })}
                >
                  {open[index] ? 'Hide evidence' : 'Show evidence'}
                </button>
                {open[index] && <blockquote>{`“${item.detail}”`}</blockquote>}
              </>
            )}
          </li>
        ))}
      </ol>
    </>
  );
}

function EmailThread({ card }) {
  const last = card.messages.length - 1;
  const [open, setOpen] = useState({ [last]: true });
  const subject = card.messages.find((item) => item.subject)?.subject;
  return (
    <>
      <div className="chat-mail-subject">
        {subject || `${card.messages.length} message${card.messages.length === 1 ? '' : 's'}`}
      </div>
      <div className="chat-mail">
        {card.messages.map((message, index) => {
          const sender = message.direction === 'out' ? 'Warehouse team (us)'
            : String(message.sender || 'Contact').replace(/_/g, ' ');
          return (
            // eslint-disable-next-line react/no-array-index-key
            <article key={index} className={`chat-mail-item ${message.direction === 'out' ? 'outbound' : 'inbound'}`}>
              <button
                type="button"
                className="chat-mail-row"
                aria-expanded={!!open[index]}
                onClick={() => setOpen({ ...open, [index]: !open[index] })}
              >
                <span className="chat-mail-avatar" aria-hidden="true">{sender.charAt(0).toUpperCase()}</span>
                <span className="chat-mail-middle">
                  <span className="chat-mail-sender">{sender}</span>
                  <span className="chat-mail-snippet">{message.body.replace(/\s+/g, ' ').slice(0, 90)}</span>
                </span>
                <time>{formatCell('date', message.at)}</time>
              </button>
              {open[index] && <div className="chat-mail-body">{message.body || '(no text)'}</div>}
            </article>
          );
        })}
      </div>
    </>
  );
}

function sortState(sort, index) {
  if (sort.at !== index) return 'none';
  return sort.descending ? 'descending' : 'ascending';
}

function sortArrow(sort, index) {
  if (sort.at !== index) return '';
  return sort.descending ? ' ↓' : ' ↑';
}

function DataTable({ card }) {
  const pageSize = 10;
  const [query, setQuery] = useState('');
  const [sort, setSort] = useState({ at: null, descending: false });
  const [page, setPage] = useState(0);
  let rows = card.rows.filter((row) => !query
    || row.some((value) => String(value ?? '').toLowerCase().includes(query.toLowerCase())));
  if (sort.at !== null) {
    const { type } = card.columns[sort.at];
    rows = [...rows].sort((a, b) => {
      const left = type === 'number' ? Number(a[sort.at]) : String(a[sort.at] ?? '');
      const right = type === 'number' ? Number(b[sort.at]) : String(b[sort.at] ?? '');
      const order = left > right ? 1 : -1;
      return (left === right ? 0 : order) * (sort.descending ? -1 : 1);
    });
  }
  const pages = Math.max(1, Math.ceil(rows.length / pageSize));
  const current = Math.min(page, pages - 1);
  return (
    <>
      <input
        type="search"
        className="chat-table-filter"
        placeholder="Filter rows"
        aria-label={`Filter ${card.title}`}
        value={query}
        onChange={(event) => { setQuery(event.target.value); setPage(0); }}
      />
      <div className="chat-table-wrap">
        <table>
          <thead>
            <tr>
              {card.columns.map((column, index) => (
                <th
                  key={column.name}
                  scope="col"
                  className={column.type === 'number' ? 'numeric' : ''}
                  aria-sort={sortState(sort, index)}
                >
                  <button
                    type="button"
                    onClick={() => setSort({
                      at: index,
                      descending: sort.at === index ? !sort.descending : column.type === 'number',
                    })}
                  >
                    {column.label}
                    {sortArrow(sort, index)}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.slice(current * pageSize, current * pageSize + pageSize).map((row, rowIndex) => (
              // eslint-disable-next-line react/no-array-index-key
              <tr key={rowIndex}>
                {row.map((value, index) => (
                  <td key={card.columns[index].name} className={card.columns[index].type === 'number' ? 'numeric' : ''}>
                    <RecordValue
                      card={card}
                      entity={card.columns[index].entity}
                      value={value}
                      type={card.columns[index].type}
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="chat-table-pager">
        <span>
          {`${rows.length} of ${card.rows.length} row${card.rows.length === 1 ? '' : 's'}`}
          {card.truncated ? ' · more exist; narrow the question' : ''}
        </span>
        {rows.length > pageSize && (
          <>
            <button type="button" className="chat-link" disabled={current === 0} onClick={() => setPage(current - 1)}>‹ Prev</button>
            <span>{`${current + 1} / ${pages}`}</span>
            <button type="button" className="chat-link" disabled={current + 1 >= pages} onClick={() => setPage(current + 1)}>Next ›</button>
          </>
        )}
      </div>
    </>
  );
}

function BarChart({ card }) {
  const [byValue, setByValue] = useState(true);
  const max = Math.max(1, ...card.bars.map((bar) => Number(bar.value) || 0));
  const bars = [...card.bars].sort((a, b) => (byValue ? Number(b.value) - Number(a.value)
    : String(a.label).localeCompare(String(b.label))));
  return (
    <>
      <div className="chat-chart-head">
        <span>{`${card.value_label} by ${String(card.label).toLowerCase()}`}</span>
        <button type="button" className="chat-link" onClick={() => setByValue(!byValue)}>
          {byValue ? 'Sort by name' : 'Sort by value'}
        </button>
      </div>
      <div className="chat-bars" role="list">
        {bars.map((bar) => (
          <div
            className="chat-bar-row"
            role="listitem"
            key={bar.label}
            title={`${card.label}: ${bar.label} · ${card.value_label}: ${formatCell('number', bar.value)}`}
          >
            <span className="chat-bar-label">
              <RecordValue card={card} entity={card.entity} value={bar.label} type="string" />
            </span>
            <span className="chat-bar-track">
              <span className="chat-bar-fill" style={{ width: `${Math.max(2, ((Number(bar.value) || 0) / max) * 100)}%` }} />
            </span>
            <span className="chat-bar-value">{formatCell('number', bar.value)}</span>
          </div>
        ))}
      </div>
    </>
  );
}

function StatCards({ card }) {
  return (
    <div className="chat-stats">
      {card.stats.map((stat) => (
        <div className="chat-stat" key={stat.label}>
          <strong>{formatCell(stat.type, stat.value)}</strong>
          <span>{stat.label}</span>
        </div>
      ))}
    </div>
  );
}

function Detail({ card }) {
  return (
    <dl className="chat-detail">
      {card.fields.map((field) => (
        <React.Fragment key={field.label}>
          <dt>{field.label}</dt>
          <dd>
            <RecordValue card={card} entity={field.entity} value={field.value} type={field.type} />
          </dd>
        </React.Fragment>
      ))}
    </dl>
  );
}

// Next steps Jev ranked for this answer. A button only sends its message; the chat
// then decides again what runs and every guardrail applies. A step the user's role
// cannot run is shown with the reason instead of a button.
export function NextSteps({ card, disabled, onAsk }) {
  const steps = (card.steps || []).filter((step) => step.label);
  if (!steps.length) return null;
  return (
    <section className="chat-next-steps" aria-label="Suggested next steps">
      <span className="chat-next-steps-title">Suggested next steps</span>
      <div className="chat-next-steps-list">
        {steps.map((step) => (
          <ActionButton key={step.capability} step={step} disabled={disabled} onAsk={onAsk} />
        ))}
      </div>
    </section>
  );
}

const VIEWS = {
  timeline: Timeline,
  email_thread: EmailThread,
  table: DataTable,
  bar_chart: BarChart,
  stat_cards: StatCards,
  detail: Detail,
};

function ActionButton({ step, disabled, onAsk }) {
  if (step.kind !== 'ask') {
    return (
      <span className={`chat-next-step is-locked effect-${step.effect}`} title={step.reason}>
        {step.label}
        <span className="chat-next-step-effect">{step.reason || 'Needs more access'}</span>
      </span>
    );
  }
  return (
    <button
      type="button"
      className={`chat-next-step effect-${step.effect}`}
      title={step.prompt}
      disabled={disabled}
      onClick={() => onAsk(step.prompt)}
    >
      {step.label}
      {step.outcome && <span className="chat-next-step-effect">{step.outcome}</span>}
    </button>
  );
}

const TONE_LABELS = { attention: 'Needs attention', good: 'Healthy', info: 'Note' };

// The answer as findings. Text and links come from the sidecar: the writer's
// sentences with record links built by code from OpenBoxes ids.
export function Insights({ card, disabled, onAsk }) {
  return (
    <div className="chat-insights">
      {card.lead && (
        <div
          className="operator-chat-richtext chat-insights-lead"
          // eslint-disable-next-line react/no-danger
          dangerouslySetInnerHTML={{ __html: renderChatMarkdown(card.lead) }}
        />
      )}
      <ul className="chat-insight-list">
        {(card.items || []).map((item, index) => (
          // eslint-disable-next-line react/no-array-index-key
          <li key={index} className={`chat-insight tone-${item.tone}`}>
            <span className="chat-insight-tone">{TONE_LABELS[item.tone] || 'Note'}</span>
            <div
              className="operator-chat-richtext chat-insight-text"
              // eslint-disable-next-line react/no-danger
              dangerouslySetInnerHTML={{ __html: renderChatMarkdown(item.text) }}
            />
            {!!item.records?.length && (
              <div className="chat-insight-records">
                {item.records.map((record) => (record.href?.startsWith('/openboxes/') ? (
                  <a key={record.label} className="chat-record-chip" href={record.href} target="_blank" rel="noopener noreferrer">
                    {record.label}
                  </a>
                ) : (
                  <span key={record.label} className="chat-record-chip">{record.label}</span>
                )))}
              </div>
            )}
            {item.action && (
              <div className="chat-insight-action">
                <ActionButton step={item.action} disabled={disabled} onAsk={onAsk} />
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function ChatComponent({ card }) {
  const View = VIEWS[card.component] || DataTable;
  return (
    <section className={`chat-component component-${card.component}`}>
      <header>
        <span className="chat-component-icon" aria-hidden="true">{ICONS[card.component] || '•'}</span>
        <strong>{card.title}</strong>
        <span className="chat-component-kind">{String(card.component).replace('_', ' ')}</span>
      </header>
      {/* Summaries computed by the sidecar from these rows; Jev chose which to show. */}
      {(card.views || []).map((view) => {
        const Summary = VIEWS[view.component];
        return Summary ? (
          <div className={`chat-component-view view-${view.component}`} key={view.title}>
            {view.component === 'bar_chart' && <span className="chat-view-title">{view.title}</span>}
            <Summary card={view} />
          </div>
        ) : null;
      })}
      <View card={card} />
    </section>
  );
}

const cell = PropTypes.oneOfType([PropTypes.string, PropTypes.number, PropTypes.bool]);
const column = PropTypes.shape({
  name: PropTypes.string, label: PropTypes.string, type: PropTypes.string,
});
const stat = PropTypes.shape({ label: PropTypes.string, value: cell, type: PropTypes.string });

RecordValue.propTypes = {
  card: PropTypes.shape({ links: PropTypes.objectOf(PropTypes.string) }).isRequired,
  entity: PropTypes.string,
  value: cell,
  type: PropTypes.string,
};
RecordValue.defaultProps = { entity: null, value: null, type: 'string' };
const actionStep = PropTypes.shape({
  capability: PropTypes.string,
  label: PropTypes.string,
  prompt: PropTypes.string,
  kind: PropTypes.string,
  effect: PropTypes.string,
  outcome: PropTypes.string,
  reason: PropTypes.string,
});
ActionButton.propTypes = {
  step: actionStep.isRequired,
  disabled: PropTypes.bool,
  onAsk: PropTypes.func.isRequired,
};
ActionButton.defaultProps = { disabled: false };
Insights.propTypes = {
  card: PropTypes.shape({
    lead: PropTypes.string,
    items: PropTypes.arrayOf(PropTypes.shape({
      text: PropTypes.string,
      tone: PropTypes.string,
      records: PropTypes.arrayOf(PropTypes.shape({
        label: PropTypes.string, href: PropTypes.string,
      })),
      action: actionStep,
    })),
  }).isRequired,
  disabled: PropTypes.bool,
  onAsk: PropTypes.func.isRequired,
};
Insights.defaultProps = { disabled: false };
NextSteps.propTypes = {
  card: PropTypes.shape({
    steps: PropTypes.arrayOf(PropTypes.shape({
      capability: PropTypes.string,
      label: PropTypes.string,
      prompt: PropTypes.string,
      kind: PropTypes.string,
      effect: PropTypes.string,
      reason: PropTypes.string,
    })),
  }).isRequired,
  disabled: PropTypes.bool,
  onAsk: PropTypes.func.isRequired,
};
NextSteps.defaultProps = { disabled: false };
ChatComponent.propTypes = {
  card: PropTypes.shape({ component: PropTypes.string, title: PropTypes.string }).isRequired,
};
Timeline.propTypes = {
  card: PropTypes.shape({
    items: PropTypes.arrayOf(PropTypes.shape({
      at: PropTypes.string,
      title: PropTypes.string,
      detail: PropTypes.string,
      tag: PropTypes.string,
    })),
  }).isRequired,
};
EmailThread.propTypes = {
  card: PropTypes.shape({
    messages: PropTypes.arrayOf(PropTypes.shape({
      sender: PropTypes.string,
      direction: PropTypes.string,
      at: PropTypes.string,
      subject: PropTypes.string,
      body: PropTypes.string,
    })),
  }).isRequired,
};
DataTable.propTypes = {
  card: PropTypes.shape({
    title: PropTypes.string,
    truncated: PropTypes.bool,
    columns: PropTypes.arrayOf(column),
    rows: PropTypes.arrayOf(PropTypes.arrayOf(cell)),
  }).isRequired,
};
BarChart.propTypes = {
  card: PropTypes.shape({
    label: PropTypes.string,
    value_label: PropTypes.string,
    bars: PropTypes.arrayOf(PropTypes.shape({ label: PropTypes.string, value: cell })),
  }).isRequired,
};
StatCards.propTypes = {
  card: PropTypes.shape({
    stats: PropTypes.arrayOf(stat),
  }).isRequired,
};
Detail.propTypes = {
  card: PropTypes.shape({
    fields: PropTypes.arrayOf(stat),
  }).isRequired,
};
