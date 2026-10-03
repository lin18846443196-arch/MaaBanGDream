"""Only shared playback behavior; room navigation stays with each task."""
from __future__ import annotations


def is_multiplayer_mode(mode: str | None) -> bool:
    return str(mode or '').strip().lower() in {'cooperative', 'team'}


def uses_multiplayer_start_gate(mode: str | None) -> bool:
    value = str(mode or '').strip().lower()
    return value.startswith('cooperative') or value == 'team-playfield-confirmed'
