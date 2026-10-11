"""One clock is shared by gateway, baseline and the real engine."""
class FakeClock:
    def __init__(self, wall=1791806400.0):  # 2026-10-12 12:00 UTC
        self.wall, self.elapsed = wall, 0.0
    def now(self): return self.wall
    def monotonic(self): return self.elapsed
    def advance(self, seconds):
        if seconds < 0: raise ValueError('clock cannot go backwards')
        self.wall += seconds
        self.elapsed += seconds
