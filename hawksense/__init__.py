"""HawkSense - a smart price tracker.

Tracks an item across local (Israeli) and worldwide online stores, computes the
real landed cost (shipping, customs duty, VAT, clearance fees), and advises
whether to buy now or wait for an upcoming sales day - with a confidence score.
"""

import os as _os

__version__ = "0.1.0"

# HawkSense was called HawkDrop: keep old HAWKDROP_* environment variables working
for _name, _value in list(_os.environ.items()):
    if _name.startswith("HAWKDROP_"):
        _os.environ.setdefault("HAWKSENSE_" + _name[len("HAWKDROP_"):], _value)
