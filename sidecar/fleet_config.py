"""Synthetic fleet contacts and policy; no OpenBoxes rows or vendor accounts."""

TEST_FLEET_EMAIL = 'batra.shivam40@gmail.com'

VENDORS = (
    {'id': 'sandbox-tire-01', 'name': 'Windy City Mobile Tire Service',
     'services': ('tire',), 'simulated_eta_minutes': 45, 'simulated_callout_usd': 180,
     'email': TEST_FLEET_EMAIL},
    {'id': 'sandbox-repair-01', 'name': 'Calumet Heavy Truck Repair',
     'services': ('mechanic', 'electrical'), 'simulated_eta_minutes': 70,
     'simulated_callout_usd': 240, 'email': TEST_FLEET_EMAIL},
    {'id': 'sandbox-tow-01', 'name': 'Lakeshore Heavy Towing',
     'services': ('tow',), 'simulated_eta_minutes': 60, 'simulated_callout_usd': 350,
     'email': TEST_FLEET_EMAIL},
    {'id': 'sandbox-cleanup-01', 'name': 'Midwest Roadside Cleanup',
     'services': ('cleanup',), 'simulated_eta_minutes': 55,
     'simulated_callout_usd': 450, 'email': TEST_FLEET_EMAIL},
    {'id': 'sandbox-cleanup-02', 'name': 'Prairie State Spill Response',
     'services': ('cleanup',), 'simulated_eta_minutes': 75,
     'simulated_callout_usd': 520, 'email': TEST_FLEET_EMAIL},
    {'id': 'sandbox-inspection-01', 'name': 'Great Lakes Cargo Inspection',
     'services': ('inspection',), 'simulated_eta_minutes': 90,
     'simulated_callout_usd': 300, 'email': TEST_FLEET_EMAIL},
)

# Reliability is a synthetic prior for quote comparison. It is not a measured
# carrier safety score or a claim about a real transport provider.
SHIPPERS = (
    {'id': 'sandbox-shipper-a', 'name': 'Midwest Fleet Services',
     'modes': ('FTL', 'PTL'), 'reliability': 0.97, 'operating_currency': 'USD',
     'email': TEST_FLEET_EMAIL, 'communication_preference': 'email',
     'yard': 'Gary, IN', 'aliases': ('Midwest Fleet A', 'Carrier A')},
    {'id': 'sandbox-shipper-b', 'name': 'Chicago Freight Partners',
     'modes': ('FTL',), 'reliability': 0.88, 'operating_currency': 'USD',
     'email': TEST_FLEET_EMAIL, 'communication_preference': 'email',
     'yard': 'Cicero, IL', 'aliases': ('Chicago Fleet B', 'Carrier B')},
    {'id': 'sandbox-shipper-c', 'name': 'Shared Load Logistics',
     'modes': ('PTL',), 'reliability': 0.92, 'operating_currency': 'USD',
     'email': TEST_FLEET_EMAIL, 'communication_preference': 'email',
     'yard': 'Joliet, IL', 'aliases': ('Shared Load C', 'Carrier C')},
)

# A vendor with a quote API can use communication_preference='api' or 'auto'
# and quote_api={'followup_url': 'https://...', 'token_env': '...'}.
# These synthetic carriers have no API endpoint; their configured channel is email.

# Synthetic, city-level places for route checks. A carrier's yard is used only until its
# own reply says where its truck would start; a warehouse's town only when OpenBoxes has
# no address for it. Neither is a real business address.
WAREHOUSE_PLACES = {'Chicago Distribution Center': 'Elk Grove Village, IL',
                    'Milwaukee Regional Warehouse': 'Milwaukee, WI'}

RECOVERY_DELAY_HOURS = 6
HIGH_STAKES_LATE_FEE_USD = 5000
CONDITIONAL_QUOTE_WINDOW_MINUTES = 30
