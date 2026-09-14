"""No candidate is left unjudged.

Gates the judge's ``signal_completion``: every URL it was handed must come
back in ``aceptadas`` or in ``descartadas``, in exactly one of them.

WHY A GATE AND NOT JUST THE PERSONA. Without this, the cheapest way to
answer is to return two accepted and say nothing about the other eight:
it validates against the schema, it looks like finished work, and those
eight vanish without anyone knowing whether they were even looked at.
And since the discard list is what stops the same URL being re-judged
tomorrow, a discard that goes unwritten is paid for on every future
search.

The candidate list is NOT read from the payload — which is what the model
writes, and therefore what it could dress up — but from ``agent_params``,
which is what the driver passed when it created the session.

The error strings stay in Spanish because the judge reads them: they are
model-facing prose, like its persona and its schema descriptions.

Ported verbatim from jaato-escriba's scripts/processors/all_judged.py.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from jaato_sdk.cascade_authoring import ProcessorResult


def _urls(items: Any) -> List[str]:
    return [str(x.get("url", "")).strip()
            for x in (items or []) if isinstance(x, dict)]


def validate(payload: Dict[str, Any], context: Any) -> ProcessorResult:
    # `agent_params` is Dict[str, str] by contract, so the candidates arrive
    # as JSON and are parsed. ONE path: if they ever arrived as a list this
    # would fail loudly rather than silently walking characters, which is
    # how the original bug was found.
    raw = (getattr(context, "agent_params", None) or {}).get("urls")
    try:
        given = [str(u).strip() for u in json.loads(raw)] if raw else []
    except (TypeError, ValueError) as exc:
        return {"faults": [
            f"agent_params['urls'] no es JSON parseable ({type(exc).__name__}), "
            f"así que no se puede comprobar la cobertura. Es un fallo de quien "
            f"creó esta sesión, no tuyo: no lo arregla otro intento."
        ]}
    if not given:
        # With no declared candidates there is nothing to reconcile. Not the
        # judge's fault: the driver did not pass them, and the judge cannot
        # fix that.
        return {"faults": [
            "agent_params no trae `urls`, así que no se puede comprobar que "
            "hayas juzgado todos los candidatos. Es un fallo de quien creó "
            "esta sesión, no tuyo: no lo arregla otro intento."
        ]}

    accepted, discarded = _urls(payload.get("aceptadas")), _urls(payload.get("descartadas"))
    seen = accepted + discarded
    errors: List[str] = []

    missing = [u for u in given if u not in seen]
    if missing:
        errors.append(
            f"{len(missing)} candidato(s) sin juzgar: {', '.join(missing[:5])}"
            f"{' …' if len(missing) > 5 else ''}. Cada URL de la lista tiene "
            f"que salir en `aceptadas` o en `descartadas`. Si no vale, "
            f"descártala con su motivo — descartar es una respuesta, callar no."
        )

    both = [u for u in set(seen) if seen.count(u) > 1]
    if both:
        errors.append(
            f"{len(both)} URL(s) en las dos listas a la vez: "
            f"{', '.join(both[:5])}. Cada una va en una sola."
        )

    invented = [u for u in seen if u not in given]
    if invented:
        errors.append(
            f"{len(invented)} URL(s) que no estaban entre los candidatos: "
            f"{', '.join(invented[:5])}. Copia las URLs tal cual de la "
            f"lista; no las retoques ni añadas otras."
        )

    return {"errors": errors}
