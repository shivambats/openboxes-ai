import React, { Component } from 'react';

import PropTypes from 'prop-types';

import apiClient from 'utils/apiClient';

const agentBase = () => window.OPENBOXES_AGENT_API_URL || '/api/agent';
const label = (value) => (value || 'unknown').replace(/_/g, ' ');

class AgentActivityPanel extends Component {
  constructor(props) {
    super(props);
    this.state = {
      loading: true, error: null, runs: [], selectedRunId: null, events: [], shipmentId: '', starting: false,
    };
  }

  componentDidMount() { this.loadRuns(); }

  componentDidUpdate(prevProps) {
    if (prevProps.locationId !== this.props.locationId) this.loadRuns();
  }

  loadRuns = () => {
    if (!this.props.locationId) return Promise.resolve(this.setState({ loading: false, runs: [] }));
    this.setState({ loading: true, error: null });
    return apiClient.get(`${agentBase()}/runs`, { params: { location_id: this.props.locationId } })
      .then(({ data }) => this.setState({ loading: false, runs: data.data || data || [] }))
      .catch(() => this.setState({ loading: false, error: 'The agent service is unavailable. Warehouse data remains unchanged.' }));
  };

  selectRun = (run) => {
    this.setState({ selectedRunId: run.id, events: [] });
    apiClient.get(`${agentBase()}/runs/${run.id}/events`)
      .then(({ data }) => this.setState({ events: data.data || data || [] }))
      .catch(() => this.setState({ events: [] }));
  };

  startInvestigation = (event) => {
    event.preventDefault();
    const { shipmentId } = this.state;
    if (!shipmentId.trim()) return;
    this.setState({ starting: true, error: null });
    apiClient.post(`${agentBase()}/runs`, { shipmentId: shipmentId.trim() })
      .then(() => this.setState({ shipmentId: '', starting: false }, this.loadRuns))
      .catch(({ response }) => this.setState({
        starting: false,
        error: (response && response.data && response.data.error) || 'Could not start this investigation. Warehouse data remains unchanged.',
      }));
  };

  render() {
    const {
      loading, error, runs, selectedRunId, events, shipmentId, starting,
    } = this.state;
    return (
      <section className="agent-activity-panel" aria-labelledby="agent-activity-title">
        <div className="agent-activity-heading">
          <div>
            <h2 id="agent-activity-title">
              <i className="fa fa-search" aria-hidden="true" />
              {' '}
              Shipment follow-ups
            </h2>
            <p>Evidence-based recommendations for delayed incoming shipments.</p>
          </div>
          <button type="button" className="btn btn-outline-secondary btn-sm" onClick={this.loadRuns} disabled={loading}>Refresh</button>
        </div>
        <form className="agent-start-form" onSubmit={this.startInvestigation}>
          <label htmlFor="agent-shipment-id">Incoming shipment ID</label>
          <input id="agent-shipment-id" value={shipmentId} onChange={(event) => this.setState({ shipmentId: event.target.value })} placeholder="Paste shipment ID" disabled={starting} />
          <button type="submit" className="btn btn-primary btn-sm" disabled={starting || !shipmentId.trim()}>{starting ? 'Starting…' : 'Investigate'}</button>
        </form>
        {loading && <p className="agent-activity-message">Checking active investigations…</p>}
        {error && <p className="agent-activity-message agent-activity-error" role="status">{error}</p>}
        {!loading && !error && runs.length === 0 && <p className="agent-activity-message">No shipment follow-ups need review right now.</p>}
        {!loading && !error && runs.length > 0 && (
        <ul className="agent-activity-runs">
          {runs.map((run) => {
            const proposal = run.latest_proposal; return (
              <li className="agent-activity-run" key={run.id}>
                <button type="button" className="agent-run-select" onClick={() => this.selectRun(run)}>
                  <strong>{run.shipment_reference}</strong>
                  <span className={`agent-status agent-status-${run.status}`}>{label(run.status)}</span>
                </button>
                <p>{proposal ? proposal.rationale : 'The agent is gathering evidence.'}</p>
                {proposal && (
                <span className="agent-proposal">
                  Proposal:
                  {proposal.title}
                </span>
                )}
                {proposal && proposal.approval.required && <span className="agent-approval-required">Review required before any action</span>}
                {selectedRunId === run.id && <ol className="agent-event-list">{events.map((event) => <li key={event.id}>{event.summary}</li>)}</ol>}
              </li>
            );
          })}
        </ul>
        )}
      </section>
    );
  }
}

AgentActivityPanel.propTypes = { locationId: PropTypes.string };
AgentActivityPanel.defaultProps = { locationId: '' };
export default AgentActivityPanel;
