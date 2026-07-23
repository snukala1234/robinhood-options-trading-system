"""Phase G — deterministic position management (spec Section 10).

Everything here is pure code. The five exit dimensions, the DTE/assignment
checkpoints, and the Section 10.6 emergency exits evaluate validated market
state and fire with NO LLM dependency — the model layer can be entirely
unavailable and every risk-reducing path still works (test-enforced: nothing
in this package imports the agents layer). The Position Manager agent's
recommendations can add urgency on top of these rules; they can never veto or
delay a deterministic emergency exit.
"""
