import React, { useRef, useState } from 'react';

import PropTypes from 'prop-types';

// OpenBoxes's own header and footer, hidden inside the pane: the dashboard already
// shows them around it.
const EMBEDDED_PAGE_STYLE = '#main-wrapper, #ft, .page-layout > header, .page-layout > footer '
  + '{ display: none !important; }';

// A record page opened from the chat, shown in place of the dashboard cards so the chat
// stays put. Links inside the page navigate within the pane.
const RecordPane = ({ href, onClose }) => {
  const frameRef = useRef(null);
  const [title, setTitle] = useState('');
  const [current, setCurrent] = useState(href);

  const onLoad = () => {
    let view;
    try {
      view = frameRef.current.contentWindow;
      // Reading the document throws if the page left this origin.
      if (!view.document) return;
    } catch (error) {
      return;
    }
    const path = view.location.pathname + view.location.search;
    // Never nest the dashboard (and its chat) inside itself or leave OpenBoxes here.
    if (!view.location.pathname.startsWith('/openboxes/')
      || view.location.pathname === window.location.pathname) {
      onClose();
      return;
    }
    const style = view.document.createElement('style');
    style.textContent = EMBEDDED_PAGE_STYLE;
    view.document.head.appendChild(style);
    setTitle(view.document.title || '');
    setCurrent(path);
  };

  return (
    <section className="record-pane" aria-label="OpenBoxes record">
      <div className="record-pane-bar">
        <button type="button" className="record-pane-back" onClick={onClose}>
          <i className="fa fa-arrow-left" aria-hidden="true" />
          <span>Back to dashboard</span>
        </button>
        <span className="record-pane-title" title={title}>{title}</span>
        <a
          className="record-pane-external"
          href={current}
          target="_blank"
          rel="noopener noreferrer"
          title="Open in a new tab"
          aria-label="Open in a new tab"
        >
          <i className="fa fa-external-link" aria-hidden="true" />
        </a>
      </div>
      <iframe
        ref={frameRef}
        className="record-pane-frame"
        src={href}
        title={title || 'OpenBoxes record'}
        onLoad={onLoad}
      />
    </section>
  );
};

RecordPane.propTypes = {
  href: PropTypes.string.isRequired,
  onClose: PropTypes.func.isRequired,
};

export default RecordPane;
