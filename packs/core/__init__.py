"""Generic third-party-logistics vocabulary pack.

This pack is deliberately restriction-free: it exists to prove the default path is
industry-free (§8). It documents the tag conventions the generic scenarios use and
contributes no constraints, no segregation pairs, and no attribute schemas.

Tag conventions (namespaces, not an enum — scenarios may extend freely):
- capabilities:  `cross-dock`, `bonded`, `hazmat-licensed`, ...
- equipment:     `equip:forklift`, `equip:reach-stacker`, `equip:crane`, ...
- certifications:`cert:<name>` with validity windows on the facility
- security:      `security:fenced`, `security:guarded`, ...
"""

from nodal.rules.framework import Pack

PACK = Pack(name="core")
