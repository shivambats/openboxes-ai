"""Opt-in real Chromium test; does not depend on carrier credentials or OpenBoxes."""
import os
import socket
import threading
import time
import pytest
from config import Config
from tools.web_surfer import WebSurferTool


@pytest.mark.skipif(os.getenv('RUN_BROWSER_TESTS') != '1', reason='Set RUN_BROWSER_TESTS=1 to launch Chromium')
def test_real_portal_browser_path():
    import uvicorn
    from mock_server import app
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        origin = f'http://127.0.0.1:{port}'
        config = Config(portal_origins=(origin,))
        result = WebSurferTool(config).check_status_via_web(origin + '/mock-carrier-portal', 'LTL-998877')
        assert result.status == 'DELAYED'
        assert result.revised_eta == '2026-09-24T10:00:00Z'
        assert result.current_location == 'Columbus, OH Terminal'
        with pytest.raises(Exception):
            WebSurferTool(config).check_status_via_web(origin + '/mock-carrier-portal', 'WRONG')
    finally:
        server.should_exit = True
        thread.join(timeout=5)
