"""V2 execution layer (spec Phase D).

The only code allowed to talk to a broker. Reasoning agents never import from this
package (test-enforced from Phase E on); orders reach a broker exclusively through
the deterministic adapters here, and only after the Phase F trade gate issues an
approval token. Throughout this build ``ORDER_MODE="research_only"`` and
``ALLOW_LIVE_ORDERS=False`` make every live submission path raise before any
transport call is made.
"""
