"""Rendering rejection data into human sentences (§7.2, §7.5).

Templates are keyed by constraint id; packs merge their own in. Rendering is a pure
function of (id, data) — wording changes never touch the event log.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nodal.rules.framework import Pack, Reject

CORE_TEMPLATES: dict[str, str] = {
    "REQUIRED_TAGS": "missing required capabilities: {missing}",
    "CERT_INVALID": "certifications not valid at arrival ({eta}): {tags}",
    "EQUIPMENT_MISSING": "required equipment not present at this facility: {tags}",
    "EQUIPMENT_DOWN": "required equipment unavailable during the stay: {tags} ({disruptions})",
    "FACILITY_CLOSED": "facility closed by {disruption} overlapping the stay",
    "ORIGIN_CLOSED": "origin {facility} is closed by {disruption} until {until};"
    " nothing can be routed out of it before then",
    "ENTRY_CLOSED": "the goods would enter through {facility}, closed by {disruption}",
    "TRANSIT_CLOSED": "the goods would pass through {facility}, closed by {disruption}",
    "EXIT_CLOSED": "the last mile would leave {facility}, closed by {disruption}",
    "STOP_EQUIPMENT_DOWN": "required equipment unavailable at the {role} stop {facility}:"
    " {tags} ({disruptions})",
    "STOP_NO_STAGING": "no zone at the {role} stop {facility} can stage the goods (tried {zones})",
    "STOP_NO_OPERATING_WINDOW": "the {role} stop {facility} never opens within the horizon",
    "ZONE_OFFLINE": "zone offline by {disruption} overlapping the stay",
    "NO_ROUTE": "no route from {origin} (origin has no coordinates and no lanes)",
    "LANE_BLOCKED": "every lane path is blocked by active disruptions ({blocked})",
    "NO_OPERATING_WINDOW": "facility {facility} has no operating window within the horizon",
    "DEADLINE_UNREACHABLE": "earliest effective arrival {eta} is after the deadline {deadline}",
    "DELIVERY_DEADLINE_UNREACHABLE": "holding here delivers to {destination} at {delivered_at},"
    " after the deadline {deadline}",
    "ZONE_KIND": "zone kind {kind} not among required kinds {wanted}",
    "TEMP_RANGE": "required temperature {required} not within zone range {zone_range}",
    "CLASS_NOT_ALLOWED": "compat class {compat_class} not allowed in this zone",
    "SEGREGATION": "class {compat_class} must not share a zone with {conflicts}",
    "CAPACITY": "needs {need} {dimension} but only {headroom} free on {day}",
    "NO_ELIGIBLE_ZONE": "no storage zone passes the zone-level constraints",
}


def render_reject(reject: "Reject", packs: Sequence["Pack"] = ()) -> str:
    templates = dict(CORE_TEMPLATES)
    for pack in packs:
        templates.update(pack.templates)
    template = templates.get(reject.constraint_id)
    if template is None:
        return str(reject.data)
    try:
        return template.format(**reject.data)
    except (KeyError, IndexError):
        return str(reject.data)
