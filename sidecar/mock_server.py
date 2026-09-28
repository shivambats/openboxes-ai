"""Synthetic LocalLTL portal. Run: uvicorn mock_server:app --host 127.0.0.1 --port 8080."""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()


@app.get('/mock-carrier-portal', response_class=HTMLResponse)
def portal(tracking: str = ''):
    form = '<form><label>Tracking <input name="tracking"></label><button type="submit">Track</button></form>'
    if tracking != 'LTL-998877':
        return '<h1>LocalLTL Sandbox</h1>' + form + '<p>No matching shipment.</p>'
    return '''<!doctype html><html><head><title>LocalLTL Sandbox</title></head><body>
    <h1>LocalLTL tracking — synthetic test data</h1>''' + form + '''
    <section data-tracking-number="LTL-998877">
    <p data-field="carrier">LocalLTL</p><p data-field="status">DELAYED</p>
    <p data-field="location">Columbus, OH Terminal</p>
    <time data-field="eta">2026-09-24T10:00:00Z</time>
    <p data-field="reason">Weather delay on I-70 East</p>
    </section></body></html>'''
