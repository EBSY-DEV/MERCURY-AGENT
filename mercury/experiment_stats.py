"""Interval estimates for experiment results.

Two methods, both closed form and well behaved at the small counts a
low-volume sender produces (zero positives, a handful of contacts):

* **Wilson score interval** for one arm's rate (Wilson 1927).
* **Newcombe's hybrid score interval** for the difference of two
  independent rates (Newcombe 1998, method 10), built from the two arms'
  Wilson intervals. Unlike the Wald interval it never leaves [-1, 1] and
  does not collapse to zero width when a rate is 0 or 1.

Both default to 95% (z = 1.959964). Tested against the worked examples in
Newcombe (1998), "Interval estimation for the difference between
independent proportions", Statistics in Medicine 17, 873-890.
"""

from __future__ import annotations

import math

Z95 = 1.959963984540054
METHOD = "Newcombe hybrid score interval (Wilson), 95%"
RATE_METHOD = "Wilson score interval, 95%"


def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """(low, high) for successes / n, or None when n is 0."""
    if n <= 0:
        return None
    if not 0 <= successes <= n:
        raise ValueError("successes must be between 0 and n")
    p = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def newcombe_difference(x1: int, n1: int, x2: int, n2: int,
                        z: float = Z95) -> tuple[float, float, float] | None:
    """(difference, low, high) for x1/n1 - x2/n2, or None when an arm is empty."""
    first, second = wilson(x1, n1, z), wilson(x2, n2, z)
    if first is None or second is None:
        return None
    p1, p2 = x1 / n1, x2 / n2
    (l1, u1), (l2, u2) = first, second
    d = p1 - p2
    low = d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    high = d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return d, max(-1.0, low), min(1.0, high)
