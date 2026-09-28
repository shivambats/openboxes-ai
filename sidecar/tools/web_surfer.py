"""Portal-specific DOM extraction; carrier HTML is data, never instructions."""
from urllib.parse import urlsplit
from models import Tracking


class WebSurferTool:
    def __init__(self, config):
        self.config = config

    def allowed(self, url):
        parsed = urlsplit(url)
        return (parsed.scheme in {'http', 'https'} and not parsed.username and not parsed.password
                and f'{parsed.scheme}://{parsed.netloc}' in self.config.portal_origins)

    def check_status_via_web(self, tracking_url, tracking_number):
        if not self.allowed(tracking_url):
            raise ValueError('Carrier portal origin is not allowlisted')
        from playwright.sync_api import sync_playwright
        with sync_playwright() as driver:
            browser = (driver.chromium.connect_over_cdp(self.config.cdp_url) if self.config.cdp_url else
                       driver.chromium.launch(headless=True, executable_path=self.config.browser_path or None))
            context = browser.new_context(service_workers='block', accept_downloads=False)
            try:
                context.route('**/*', lambda route: route.continue_() if self.allowed(route.request.url) else route.abort())
                page = context.new_page()
                page.set_default_timeout(15000)
                page.goto(tracking_url, wait_until='domcontentloaded')
                if page.locator('input[name="tracking"]').count():
                    page.locator('input[name="tracking"]').fill(tracking_number)
                    page.locator('button[type="submit"]').click()
                result = page.locator('[data-tracking-number]')
                result.wait_for()
                if result.get_attribute('data-tracking-number') != tracking_number:
                    raise ValueError('Portal returned a different tracking number')
                raw = {key: page.locator(f'[data-field="{key}"]').inner_text().strip()
                       for key in ('carrier', 'status', 'location', 'eta', 'reason')}
                raw['tracking_number'] = tracking_number
                raw['source_url'] = page.url
                return Tracking(raw['carrier'], raw['status'], raw['location'], raw['eta'], raw['reason'], raw)
            finally:
                context.close()
                browser.close()
