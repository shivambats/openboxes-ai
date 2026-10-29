import React, { Component } from 'react';

import PropTypes from 'prop-types';

import apiClient from 'utils/apiClient';

const appPath = (path) => `${(window.CONTEXT_PATH || '').replace(/\/$/, '')}${path}`;

class DashboardAttentionPanel extends Component {
  constructor(props) {
    super(props);
    this.state = { delayed: null, recalls: null, loading: false };
  }

  componentDidMount() { this.loadAttention(); }

  componentDidUpdate(prevProps) {
    if (prevProps.locationId !== this.props.locationId) this.loadAttention();
  }

  loadAttention = () => {
    const { locationId } = this.props;
    if (!locationId) return;
    this.setState({ loading: true, delayed: null, recalls: null });
    Promise.all([
      apiClient.get('/api/dashboard/delayedShipments', { params: { locationId } })
        .then(({ data }) => data.data.tableData.body || []).catch(() => null),
      apiClient.get('/api/recallReviews/summary')
        .then(({ data }) => data.data.count).catch(() => null),
    ]).then(([delayed, recalls]) => {
      if (this.props.locationId === locationId) this.setState({ delayed, recalls, loading: false });
    });
  };

  render() {
    const { delayed, recalls, loading } = this.state;
    const delayedCount = delayed && delayed.length;
    let delayedDisplay = delayedCount === null ? '—' : delayedCount;
    let recallDisplay = recalls === null ? '—' : recalls;
    if (loading) {
      delayedDisplay = '…';
      recallDisplay = '…';
    }
    return (
      <section className="dashboard-attention" aria-labelledby="dashboard-attention-title">
        <div className="dashboard-attention-heading">
          <div>
            <span className="dashboard-attention-eyebrow">WAREHOUSE PRIORITIES</span>
            <h2 id="dashboard-attention-title">Needs attention</h2>
            <p>Review incoming stock delays and lots flagged for recall review.</p>
          </div>
          <button type="button" className="dashboard-attention-refresh" onClick={this.loadAttention} disabled={loading || !this.props.locationId}>
            <i className="fa fa-refresh" aria-hidden="true" />
            {' '}
            Refresh
          </button>
        </div>
        <div className="dashboard-attention-grid">
          <article className="dashboard-attention-card dashboard-attention-card--delayed">
            <div className="dashboard-attention-card-top">
              <span className="dashboard-attention-icon"><i className="fa fa-truck" aria-hidden="true" /></span>
              <span className="dashboard-attention-tag">INCOMING</span>
            </div>
            <h3>Delayed incoming stock</h3>
            <p>Unreceived shipments past their expected arrival or transport window.</p>
            <div className="dashboard-attention-stat" aria-live="polite">
              <strong>{delayedDisplay}</strong>
              <span>{delayedCount === 1 ? 'shipment to review' : 'shipments to review'}</span>
            </div>
            {delayed && delayed.length > 0 && (
              <div className="dashboard-attention-preview">
                <span>DELAYED SHIPMENTS</span>
                {delayed.slice(0, 2).map((shipment) => (
                  <a key={shipment.link} href={shipment.link}>
                    <span>{shipment.name}</span>
                    <span>
                      {shipment.number}
                      {' '}
                      <i className="fa fa-arrow-right" aria-hidden="true" />
                    </span>
                  </a>
                ))}
              </div>
            )}
            <a className="dashboard-attention-link" href={appPath('/stockMovement/list?direction=INBOUND')}>
              View incoming stock
              <i className="fa fa-arrow-right" aria-hidden="true" />
            </a>
          </article>
          <article className="dashboard-attention-card dashboard-attention-card--recall">
            <div className="dashboard-attention-card-top">
              <span className="dashboard-attention-icon"><i className="fa fa-shield" aria-hidden="true" /></span>
              <span className="dashboard-attention-tag">INVENTORY</span>
            </div>
            <h3>Stock recall review</h3>
            <p>Product lots marked for human review after a recall check.</p>
            <div className="dashboard-attention-stat" aria-live="polite">
              <strong>{recallDisplay}</strong>
              <span>{recalls === 1 ? 'lot to review' : 'lots to review'}</span>
            </div>
            <div className="dashboard-attention-note">Review the evidence and decide whether a stock action is needed.</div>
            <a className="dashboard-attention-link" href={appPath('/inventory/browse?showNeedsAttentionOnly=true&searchPerformed=true')}>
              Review flagged lots
              <i className="fa fa-arrow-right" aria-hidden="true" />
            </a>
          </article>
        </div>
      </section>
    );
  }
}

DashboardAttentionPanel.propTypes = { locationId: PropTypes.string };
DashboardAttentionPanel.defaultProps = { locationId: '' };

export default DashboardAttentionPanel;
