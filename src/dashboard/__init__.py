"""Phase J — the read-only dashboard (spec Section 18).

Read-only is structural, not conventional, at four independent layers:

1. The API's database role has SELECT-only grants and a read-only session
   default — even a buggy route cannot mutate state at the database.
2. Nothing in this package imports the execution adapter, the gate, the
   submitter, a broker, or the agents (sweep-enforced): there is no object in
   scope that could trade.
3. Every HTTP route is GET; there are no mutating endpoints to misuse.
4. The WebSocket is server-push: inbound frames beyond connection lifecycle
   (ping/subscribe) are ignored and logged, dispatched to nothing.

Stored free text (theses, reasons, catalyst descriptions) is rendered as
escaped data, never interpreted — the Phase E injection stance extended to
the screen. Binds to localhost only (Section 17).
"""
