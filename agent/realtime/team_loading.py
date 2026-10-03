"""Separate ready delivery, member loading and final-cover time budgets."""
from __future__ import annotations


class TeamLoadingBudget:
    def __init__(self, now: float, member_timeout: float):
        self.member_timeout = float(member_timeout)
        self.started = now
        self.hard_deadline = now + 60 + 2 * self.member_timeout + 60
        self.member_started = None
        self.last_progress = None
        self.cover_started = None
        self.high_water = None
        self.candidate = None

    def observe(self, now, state, progress=None):
        if now >= self.hard_deadline:
            return f'准备至最终封面超过总上限 {int(self.hard_deadline-self.started)} 秒'
        if state != 'loading':
            self.candidate = None
        if state in {'ready_wait', 'loading'}:
            self.cover_started = None
            if self.member_started is None:
                self.member_started = self.last_progress = now
            # Require two consistent readings, then only meaningful increases
            # beyond a per-member high-water mark extend the idle timeout.
            # Stickers, falling percentages and repeated frames cannot renew it.
            if progress is not None:
                if self.candidate is not None and all(
                    abs(a-b) <= 1 for a, b in zip(progress, self.candidate)
                ):
                    if self.high_water is None:
                        self.high_water = tuple(progress)
                    elif any(a >= b+3 for a, b in zip(progress, self.high_water)):
                        self.high_water = tuple(max(a,b) for a,b in zip(progress,self.high_water))
                        self.last_progress = now
                self.candidate = tuple(progress)
            else:
                self.candidate = None
            if now >= self.member_started + 2*self.member_timeout:
                return f'成员准备／加载超过总上限 {int(2*self.member_timeout)} 秒'
            if now >= self.last_progress + self.member_timeout:
                return f'成员准备／加载连续 {int(self.member_timeout)} 秒无可确认进展'
        elif state == 'unknown' and self.member_started is not None:
            if self.cover_started is None:
                self.cover_started = now
            if now >= self.cover_started+60:
                return '成员加载页消失后，最终封面60秒超时'
        elif self.member_started is None and now >= self.started+60:
            return '准备确认／最终封面60秒超时（未确认进入成员加载页）'
        return None
