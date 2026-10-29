"""Bounded fleet action proposals from visible driver-photo evidence."""
from fleet_manager_agent import vendor_sandbox_search


ACTION_RULES = {
    'request_cleanup_team': {
        'requires': ['confirm_driver_safety', 'confirm_emergency_response',
                     'exact_incident_location', 'identify_spilled_material',
                     'scene_clearance', 'confirm_cleanup_responsibility',
                     'approved_cleanup_vendor'],
    },
    'request_inspection_team': {
        'requires': ['confirm_driver_safety', 'scene_clearance',
                     'exact_incident_location', 'qualified_vehicle_and_cargo_inspector'],
    },
    'start_insurance_intake': {
        'requires': ['confirm_fleet_insurance_contact', 'incident_time_and_location',
                     'vehicle_and_shipment_ids', 'official_report_if_available'],
    },
}


def propose_driver_photo_actions(observations):
    """Keep model choices as review tasks; never dispatch or file a claim here."""
    selected = {}
    for observation in observations:
        findings = observation['findings']
        for item in findings.get('recommended_actions', []):
            action = item.get('action')
            rule = ACTION_RULES.get(action)
            field = item.get('evidence_field')
            evidence = item.get('evidence')
            visible = findings.get(field) if field in {'condition', 'description'} else None
            if (not rule or not isinstance(evidence, str) or not evidence or
                    not isinstance(visible, (str, list)) or
                    not any(evidence.casefold() in value.casefold()
                            for value in ([visible] if isinstance(visible, str) else visible)
                            if isinstance(value, str))):
                continue
            selected.setdefault(action, []).append({
                'image_sha256': observation['sha256'],
                'field': field, 'visible_evidence': evidence})
    return [{'action': action, 'state': 'awaiting_fleet_review',
             'evidence': selected[action], 'requires': rule['requires'],
             'vendor_options': vendor_sandbox_search(
                 'cleanup' if action == 'request_cleanup_team' else 'inspection')
                 if action != 'start_insurance_intake' else []}
            for action, rule in ACTION_RULES.items() if action in selected]
